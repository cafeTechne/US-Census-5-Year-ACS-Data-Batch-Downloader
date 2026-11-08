# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-state manifests with a declared RPP adjustment (PLAN_ROUND_14 workstream A).

- Validation of ``geography.states`` and ``adjustments`` (A1, A2).
- Derivations evaluated on supplied replicates (A3) and the adjustment of
  estimates, SEs and replicates, on a small synthetic two-state fake client.
- The whole confirmatory route of examples/manifests/mi_ny_income_education.json
  offline (A4): ``cache import`` of frozen real Census API responses
  (tests/fixtures/mi_ny_manifest/, trimmed to the manifest's variables) into an
  isolated cache, then ``python -m census tool run_manifest`` and the MCP tool,
  with network access refused. County-to-CBSA membership comes from the frozen
  ``cbsa_membership.csv`` (recorded from the Census 2024 county and CBSA
  boundary overlay) instead of the boundary files, which are too large to
  freeze; the live test exercises the overlay itself.

Re-record the fixtures (live; needs CENSUS_API_KEY; never prints the key)::

    python -m tests.test_manifest_multistate --record
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from census.api import CensusAPIClient
from census.manifest import load_manifest, manifest_hash, methods_section, run_manifest, validate_manifest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANIFEST = os.path.join(ROOT, "examples", "manifests", "mi_ny_income_education.json")
MANIFEST_HASH = "d8905f57625e1d1862a63ca0f6deeb364a7643385fc4a700b6691e855e3bfcb0"
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "mi_ny_manifest")
MEMBERSHIP = os.path.join(FIXTURES, "cbsa_membership.csv")
MEMBERSHIP_SOURCE = os.path.join(FIXTURES, "cbsa_membership_source.txt")
STATES = ("26", "36")
REP_COLS = [f"rep_{i}" for i in range(1, 81)]
LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"
# Round 13 exploratory result (docs/case_study_mi_ny.md, section 3), RPP-adjusted, population target
ROUND13 = {"estimate": 811.00, "ci_lower": 684.29, "ci_upper": 937.71}


def frozen_membership():
    """(membership indexed by GEOID, boundary source) as census.workflows_microdata._county_rpp_areas returns."""
    df = pd.read_csv(MEMBERSHIP, dtype=str)
    df["share"] = pd.to_numeric(df["share"])
    with open(MEMBERSHIP_SOURCE, encoding="utf-8") as f:
        source = f.read().strip()
    return df.set_index("GEOID"), source


def patched_membership():
    from census import workflows_microdata

    membership, source = frozen_membership()

    def lookup(geoids, year):
        missing = sorted(set(geoids) - set(membership.index))
        if missing:
            raise ValueError(f"no frozen CBSA membership for {missing[:5]}")
        return membership.loc[sorted(set(geoids))], source

    return mock.patch.object(workflows_microdata, "_county_rpp_areas", side_effect=lookup)


# ---------------------------------------------------------------------------
# synthetic two-state client (no fixtures)
# ---------------------------------------------------------------------------

def _payload(table_code, state, n=12, seed=3):
    """County payload for state ``state``: B19013 income rises with the B15002 college share."""
    rng = np.random.default_rng(seed + int(state))
    total = rng.uniform(5000, 50000, n).round()
    share = rng.uniform(0.15, 0.45, n)
    college = (total * share).round()
    income = 40000 + 900 * 100 * share + rng.normal(0, 3000, n) + (3000 if state == "36" else 0)
    if table_code == "B19013":
        cols = {"B19013_001": (income, income * 0.04)}
    else:
        cols = {"B15002_001": (total, total * 0.01), "B15002_015": (college, college * 0.05)}
    header = ["GEO_ID", "NAME"] + [f"{c}{s}" for c in cols for s in ("E", "EA", "M", "MA")] + ["state", "county"]
    rows = [header]
    for i in range(n):
        county = f"{2 * i + 1:03d}"
        vals = []
        for est, moe in cols.values():
            vals += [f"{est[i]:.0f}", None, f"{moe[i]:.0f}", None]
        rows.append([f"0500000US{state}{county}", f"County {i} {state}"] + vals + [state, county])
    return rows


class TwoStateClient(CensusAPIClient):
    def __init__(self, fail_states=()):
        super().__init__(api_key="", cache=None)
        self.fail_states = set(fail_states)
        self.calls = []

    def fetch(self, year, state_code, table_code, geo_level="place", **kwargs):  # type: ignore[override]
        self.calls.append((table_code, state_code))
        if state_code in self.fail_states:
            self.last_error = "HTTP 500"
            return None
        self.last_error = None
        return _payload(table_code, state_code)

    def fetch_variable_metadata(self, year, table_code, dataset=None):  # type: ignore[override]
        return {}


def two_state_manifest(**changes):
    m = {
        "schema_version": 1, "title": "Two-state synthetic test", "authors": ["agent:test"],
        "created": "2026-11-08T00:00:00Z", "releases": [2024],
        "geography": {"level": "county", "states": ["26", "36"]},
        "variables": [{"name": "income", "ref": "2024:B19013_001"}, {"name": "total", "ref": "2024:B15002_001"},
                      {"name": "college", "ref": "2024:B15002_015"}],
        "derivations": [{"name": "pct", "op": "proportion", "num": "college", "den": "total", "as_percent": True}],
        "adjustments": [{"type": "rpp", "variables": ["income"], "rpp_year": 2023, "areas": "state"}],
        "analyses": [{"id": "m1", "method": "fit_replicate", "formula": "income ~ pct + C(GEOID.str[:2])"}],
        "hypotheses": [{"id": "H1", "analysis": "m1", "statistic": "coefficient", "term": "pct",
                        "direction": "greater"}],
    }
    m.update(changes)
    return m


class TestValidation(unittest.TestCase):
    def test_example_valid_and_hash(self):
        m = load_manifest(MANIFEST)
        self.assertEqual(validate_manifest(m), [])
        self.assertEqual(manifest_hash(m), MANIFEST_HASH)

    def test_single_state_example_hash_unchanged(self):
        from tests.test_reference_workflow import MANIFEST as SINGLE, MANIFEST_HASH as SINGLE_HASH

        self.assertEqual(manifest_hash(load_manifest(SINGLE)), SINGLE_HASH)

    def check(self, m, fragment):
        errs = validate_manifest(m, resolve=False)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_geography_errors(self):
        self.assertEqual(validate_manifest(two_state_manifest(), resolve=False), [])
        self.check(two_state_manifest(geography={"level": "county", "state": "26", "states": ["36"]}),
                   "not both")
        self.check(two_state_manifest(geography={"level": "county", "states": []}), "non-empty list")
        self.check(two_state_manifest(geography={"level": "county", "states": "26,36"}), "non-empty list")
        self.check(two_state_manifest(geography={"level": "county", "states": ["26", "26"]}), "more than once")
        self.check(two_state_manifest(geography={"level": "county", "states": ["26", "all"]}), "one 2-digit")
        self.check(two_state_manifest(geography={"level": "county", "states": ["26", "99"]}), "geography.states")
        self.check(two_state_manifest(geography={"level": "county", "states": ["26", "36"], "codes": ["001"]}),
                   "cannot be combined")
        self.check(two_state_manifest(geography={"level": "msa", "states": ["26", "36"]},
                                      adjustments=None), "does not apply")
        self.check(two_state_manifest(geography={"level": "county"}), "state_code is required")

    def test_adjustment_errors(self):
        def adj(**kw):
            return two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"], **kw}])

        self.assertEqual(validate_manifest(adj(rpp_year=2023), resolve=False), [])  # areas defaulted
        self.check(adj(), "rpp_year is required")  # create_manifest fills it in (test_review_fixes_r14)
        self.check(adj(type="cpi"), "type must be")
        self.check(two_state_manifest(adjustments=[{"type": "rpp", "variables": []}]), "non-empty list")
        self.check(two_state_manifest(adjustments=[{"type": "rpp", "variables": ["rent"]}]), "unknown variable")
        self.check(adj(rpp_year=1990), "rpp_year must be")
        self.check(adj(rpp_year="2023"), "rpp_year must be")
        self.check(adj(areas="msa"), "areas must be")
        self.check(adj(colour="red"), "unknown key")
        self.check(two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"]},
                                                   {"type": "rpp", "variables": ["income"]}]), "adjusted twice")
        self.check(two_state_manifest(releases=[2023, 2024], variables=[
            {"name": "income", "ref": "2024:B19013_001"}, {"name": "total", "ref": "2024:B15002_001"},
            {"name": "college", "ref": "2024:B15002_015"}]), "exactly one release")
        self.check(two_state_manifest(aggregation={"mapping": {"26001": "a"}}), "aggregation")
        tracts = two_state_manifest(geography={"level": "tract", "state": "26"},
                                    adjustments=[{"type": "rpp", "variables": ["income"], "areas": "county_metro"}])
        self.check(tracts, "needs geography level 'county'")
        zcta = two_state_manifest(geography={"level": "zcta", "state": "26"},
                                  adjustments=[{"type": "rpp", "variables": ["income"]}])
        self.check(zcta, "state-based geography")
        self.check(two_state_manifest(adjustments={"type": "rpp"}), "must be a list")


class TestTwoStateRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_m(self, m, client=None, **kw):
        return run_manifest(m, os.path.join(self.dir, "run"), client=client or TwoStateClient(), use_cache=False, **kw)

    def test_states_stacked_and_state_rpp(self):
        from census import rpp

        client = TwoStateClient()
        res = self.run_m(two_state_manifest(), client)
        self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
        self.assertEqual(sorted(set(client.calls)), [("B15002", "26"), ("B15002", "36"), ("B19013", "26"),
                                                    ("B19013", "36")])
        frame = res["frame"]
        self.assertEqual(len(frame), 24)
        self.assertEqual(sorted(frame["GEOID"].str[:2].unique()), list(STATES))
        raw = pd.DataFrame(_payload("B19013", "36")[1:], columns=_payload("B19013", "36")[0])
        row = frame[frame["GEOID"] == "36001"].iloc[0]
        factor = 100.0 / rpp.place_parity("36", 2023)["rpp"]
        self.assertAlmostEqual(row["income"], float(raw.loc[0, "B19013_001E"]) * factor)
        self.assertAlmostEqual(row["income_se"], float(raw.loc[0, "B19013_001M"]) / 1.645 * factor)
        adj = res["adjustments"][0]
        self.assertEqual((adj["rpp_year"], adj["rpp_year_source"], adj["dollar_year"]), (2023, "manifest", 2024))
        self.assertFalse(adj["rpp_year_matches_release"])
        self.assertTrue(any(n.startswith("RPP YEAR:") for n in adj["notes"]))
        self.assertEqual(adj["rpp_table"]["source"], "bundled")
        self.assertEqual(adj["rpp_table"]["sha256"], rpp.price_parity_info()["csv_sha256"]["rpp.csv"])
        self.assertEqual(sorted(a["rpp_area"] for a in adj["rpp_areas"]), ["26000", "36000"])
        self.assertFalse(any(d["kind"] == "adjustment_failed" for d in res["deviations"]))  # mismatch is a note
        with open(os.path.join(self.dir, "run", "bundle", "bundle.json"), encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertEqual(prov["adjustments"][0]["rpp_year"], 2023)
        self.assertEqual(prov["adjustments"][0]["rpp_table"]["source"], "bundled")
        with open(os.path.join(self.dir, "run", "bundle", "results", "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["extraction"]["B19013"]["states"], list(STATES))
        text = methods_section(two_state_manifest(), run)
        self.assertIn("states 26, 36", text)
        self.assertIn("2023 RPP was applied to dollars of 2024", text)
        self.assertIn("no automatic state effects", text)

    def test_omitted_rpp_year_is_invalid_at_run(self):
        m = two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"]}])
        res = self.run_m(m)
        self.assertFalse(res["success"])
        self.assertTrue(any("rpp_year is required" in e for e in res["errors"]), res["errors"])

    def test_default_areas_recorded(self):
        from census.manifest import latest_rpp_year

        m = two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"], "rpp_year": latest_rpp_year()}])
        with patched_membership():
            res = self.run_m(m)
        adj = res["adjustments"][0]
        self.assertEqual(adj["rpp_year_source"], "manifest")
        self.assertEqual(adj["areas"], "county_metro")  # county default

    def test_one_state_failing_is_blocking(self):
        res = self.run_m(two_state_manifest(), TwoStateClient(fail_states={"36"}))
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        self.assertIn("failed_calls", {d["kind"] for d in res["deviations"]})

    def test_adjustment_failure_is_blocking(self):
        from census import workflows_microdata

        with mock.patch.object(workflows_microdata, "_county_rpp_areas", side_effect=OSError("no boundaries")):
            res = self.run_m(two_state_manifest(adjustments=[{"type": "rpp", "variables": ["income"],
                                                              "rpp_year": 2024}]))
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        kinds = [d["kind"] for d in res["deviations"]]
        self.assertIn("adjustment_failed", kinds)
        self.assertIn("analysis_not_run", kinds)  # never run on unadjusted dollars

    def _replicates(self, frame, names_by_code=False):
        """Synthetic replicates for income, total and college (by manifest name or ACS code)."""
        rng = np.random.default_rng(11)
        parts = []
        for name, code in (("income", "B19013_001"), ("total", "B15002_001"), ("college", "B15002_015")):
            est = frame[name].to_numpy(float)
            part = pd.DataFrame({"GEOID": frame["GEOID"], "variable": code if names_by_code else name,
                                 "estimate": est})
            reps = est[:, None] * (1 + rng.normal(0, 0.02, (len(est), 80)))
            parts.append(pd.concat([part, pd.DataFrame(reps, columns=REP_COLS)], axis=1))
        return pd.concat(parts, ignore_index=True)

    def test_derivation_on_replicates_and_scaling(self):
        from census import rpp

        probe = self.run_m(two_state_manifest(adjustments=None))["frame"]  # unadjusted frame
        for by_code in (False, True):
            reps = self._replicates(probe, names_by_code=by_code)
            res = self.run_m(two_state_manifest(), replicates=reps)
            self.assertEqual(res["analysis_status"], "confirmatory", res["deviations"])
            self.assertEqual(res["replicates"]["derived_on_replicates"], ["pct"])
            self.assertEqual(res["replicates"]["scaled_by_adjustments"], ["income"])
            frame = res["frame"].set_index("GEOID")
            self.assertTrue((frame["pct_method"] == "replicate").all())
            # exact replicate SE of the derived share, evaluated from the input replicates
            g = "26003"
            sub = reps[reps["GEOID"] == g].set_index("variable")
            num = sub.loc["B15002_015" if by_code else "college", REP_COLS].to_numpy(float)
            den = sub.loc["B15002_001" if by_code else "total", REP_COLS].to_numpy(float)
            est = frame.loc[g, "pct"]
            se = np.sqrt(4 / 80 * ((100 * num / den - est) ** 2).sum())
            self.assertAlmostEqual(frame.loc[g, "pct_se"], se, places=6)
            # income kept its published SE (only derived variables take replicate SEs)
            factor = 100.0 / rpp.place_parity("26", 2023)["rpp"]
            self.assertAlmostEqual(frame.loc[g, "income_se"], probe.set_index("GEOID").loc[g, "income_se"] * factor)
            m1 = res["results"]["m1"]
            self.assertEqual(m1.method, "replicate_refit")
            self.assertEqual(sorted(m1.provenance["replicate_variables"]), ["income", "pct"])
            self.assertTrue(np.isfinite(m1.coefficients.set_index("term").loc["pct", "se_sampling"]))
        # the caller-supplied replicate hash is of the input, not of the derived/scaled frame
        from census.manifest import replicates_sha256

        self.assertEqual(res["replicates"]["sha256"], replicates_sha256(reps))

    def test_scaled_replicates_equal_scaled_estimates(self):
        """Replicates of an adjusted variable scale by the row's RPP factor (refit uses them)."""
        from census import manifest, rpp

        probe = self.run_m(two_state_manifest(adjustments=None))["frame"]
        reps = self._replicates(probe)
        seen = {}
        real = manifest._run_analysis

        def spy(a, frame, level, replicates, correction):
            seen["reps"] = replicates
            return real(a, frame, level, replicates, correction)

        with mock.patch.object(manifest, "_run_analysis", side_effect=spy):
            self.run_m(two_state_manifest(), replicates=reps)
        got = seen["reps"]
        r = got[(got["variable"] == "income") & (got["GEOID"] == "36005")].iloc[0]
        r0 = reps[(reps["variable"] == "income") & (reps["GEOID"] == "36005")].iloc[0]
        factor = 100.0 / rpp.place_parity("36", 2023)["rpp"]
        np.testing.assert_allclose(r[["estimate"] + REP_COLS].to_numpy(float),
                                   r0[["estimate"] + REP_COLS].to_numpy(float) * factor)

    def test_derivation_without_input_replicates_blocks_strict(self):
        probe = self.run_m(two_state_manifest(adjustments=None))["frame"]
        reps = self._replicates(probe)
        reps = reps[reps["variable"] != "college"]  # pct cannot be evaluated on replicates
        res = self.run_m(two_state_manifest(), replicates=reps)
        self.assertEqual(res["analysis_status"], "confirmatory_incomplete")
        self.assertIn("replicates_incomplete", {d["kind"] for d in res["deviations"]})
        self.assertNotIn("derived_on_replicates", res["replicates"])


# ---------------------------------------------------------------------------
# frozen real-data route (examples/manifests/mi_ny_income_education.json)
# ---------------------------------------------------------------------------

def run_cli(argv):
    from census.__main__ import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = main(argv)
    return code, json.loads(buf.getvalue())


class TestFrozenMiNyRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_reference_workflow import offline

        cls.dir = tempfile.mkdtemp(prefix="mi_ny_manifest_")
        cls.env = mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": os.path.join(cls.dir, "cache.db")})
        cls.env.start()
        with offline(), patched_membership():
            code, cls.imported = run_cli(["cache", "import", FIXTURES, "--json"])
            assert code == 0, cls.imported
            code, cls.cli = run_cli(["tool", "run_manifest", "--arg", f"manifest_path={MANIFEST}",
                                     "--arg", f"output_dir={os.path.join(cls.dir, 'cli')}", "--json"])
            cls.cli_code = code

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_import_is_real_data(self):
        self.assertEqual(self.imported["imported"], 4)
        self.assertFalse(self.imported["synthetic"])

    def test_confirmatory_result_reproduces_round13(self):
        out = self.cli
        self.assertEqual(self.cli_code, 0, out.get("error"))
        self.assertTrue(out["success"])
        self.assertEqual(out["analysis_status"], "confirmatory")
        self.assertEqual(out["deviations"], [])
        self.assertEqual(out["manifest_hash"], MANIFEST_HASH)
        self.assertEqual(out["data_source"]["synthetic"], False)
        h1 = out["hypotheses"][0]
        self.assertEqual((h1["id"], h1["status"], h1["se_basis"]), ("H1", "tested", "se_model"))
        for k, v in ROUND13.items():
            self.assertAlmostEqual(h1[k], v, places=2)
        self.assertTrue(h1["significant"])
        with open(os.path.join(self.dir, "cli", "bundle", "results", "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["n_units"], 145)
        adj = run["adjustments"][0]
        self.assertEqual(len(adj["rpp_areas"]), 31)  # 16 + 13 MSAs and two nonmetro portions
        rows = {a["rpp_area"]: a for a in adj["rpp_areas"]}
        self.assertEqual((rows["26999"]["rows"], rows["36999"]["rows"]), (53, 25))
        self.assertAlmostEqual(rows["35620"]["rpp"], 112.563)
        with open(os.path.join(self.dir, "cli", "bundle", "bundle.json"), encoding="utf-8") as f:
            bundle = json.load(f)
        self.assertEqual(bundle["provenance"]["analysis_status"], "confirmatory")
        self.assertEqual(bundle["provenance"]["adjustments"][0]["rpp_year"], 2024)
        self.assertIn("Retrieved via the Census Data API", bundle["citation"])

    def test_mcp_route_agrees(self):
        from tests.test_reference_workflow import _mcp_call, offline

        import mcp_server

        with offline(), patched_membership():
            out = _mcp_call(mcp_server, "run_manifest", {"manifest_path": MANIFEST,
                                                         "output_dir": os.path.join(self.dir, "mcp")})
        self.assertEqual(out["analysis_status"], "confirmatory", out.get("error"))
        self.assertEqual(out["manifest_hash"], self.cli["manifest_hash"])
        a, b = out["hypotheses"][0], self.cli["hypotheses"][0]
        for k in ("estimate", "se", "ci_lower", "ci_upper", "p_value"):
            self.assertAlmostEqual(a[k], b[k], places=9)


@unittest.skipUnless(LIVE, "set CENSUS_LIVE_TESTS=1 to run live Census API tests")
class TestLiveMiNyRoute(unittest.TestCase):
    """The same route on the live Census API and boundary files (default cache)."""

    def test_live_confirmatory_run(self):
        d = tempfile.mkdtemp(prefix="mi_ny_live_")
        self.addCleanup(shutil.rmtree, d, True)
        code, out = run_cli(["tool", "run_manifest", "--arg", f"manifest_path={MANIFEST}",
                             "--arg", f"output_dir={d}", "--json"])
        self.assertEqual(code, 0, out.get("error"))
        self.assertEqual(out["analysis_status"], "confirmatory")
        self.assertEqual(out["data_source"]["synthetic"], False)
        h1 = out["hypotheses"][0]
        for k, v in ROUND13.items():
            self.assertAlmostEqual(h1[k], v, places=2)
        # the live CBSA overlay agrees with the frozen membership used offline
        from census import workflows_microdata

        frozen, _ = frozen_membership()
        live, _ = workflows_microdata._county_rpp_areas(sorted(frozen.index), 2024)
        pd.testing.assert_series_equal(live["CBSA"].fillna("").astype(str).sort_index(),
                                       frozen["CBSA"].fillna("").astype(str).sort_index(), check_names=False)


# ---------------------------------------------------------------------------
# recording (live)
# ---------------------------------------------------------------------------

def record():
    """Fetch the four responses (2024 acs5, all counties of 26 and 36; B19013 and B15002)
    through the toolkit client, trim each to the manifest's variables, and write the
    frozen files and the CBSA membership. Never writes or prints the API key."""
    from census import workflows_microdata
    from census.manifest import tables_and_refs

    m = load_manifest(MANIFEST)
    keep = {t: [r.split(":")[1] for r in refs] for t, refs in tables_and_refs(m).items()}
    client = CensusAPIClient()
    os.makedirs(FIXTURES, exist_ok=True)
    geoids = []
    for st in STATES:
        request = {"year": 2024, "dataset": "acs5", "geo_level": "county", "state_code": st, "county_code": "*"}
        doc = {"_about": (f"REAL Census Data API responses (2024 acs5, all counties of state {st}, get=NAME,"
                          "group(<table>)), recorded by `python -m tests.test_manifest_multistate --record` for "
                          "examples/manifests/mi_ny_income_education.json; columns trimmed to the manifest's "
                          "variables (estimate, MOE and annotations) plus GEO_ID, NAME, state, county."),
               "request": request, "requests": {}}
        for table, codes in keep.items():
            rows = client.fetch(table_code=table, **request)
            if rows is None:
                raise SystemExit(f"fetch failed for {table} state {st}")
            header = rows[0]
            cols = [i for i, h in enumerate(header)
                    if h in ("GEO_ID", "NAME", "state", "county")
                    or any(h == f"{c}{s}" for c in codes for s in ("E", "EA", "M", "MA"))]
            doc["requests"][table] = [[r[i] for i in cols] for r in rows]
            if table == "B19013":
                geoids += [r[header.index("state")] + r[header.index("county")] for r in rows[1:]]
        with open(os.path.join(FIXTURES, f"frozen_api_responses_{st}.json"), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.write("\n")
    membership, source = workflows_microdata._county_rpp_areas(sorted(geoids), 2024)
    out = membership.reset_index()[["GEOID", "CBSA", "CBSA_NAME", "LSAD", "share"]].sort_values("GEOID")
    out["share"] = out["share"].round(6)
    out.to_csv(MEMBERSHIP, index=False, lineterminator="\n")
    with open(MEMBERSHIP_SOURCE, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{source}\n")
    print(f"recorded {len(geoids)} counties into {FIXTURES}")


if __name__ == "__main__":
    if "--record" in sys.argv:
        record()
    else:
        unittest.main()
