# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for census.manifest (offline: fake API client + metadata archive)."""

import copy
import json
import os
import shutil
import tempfile
import unittest

import numpy as np

from census.api import CensusAPIClient
from census.manifest import (
    STATUS_CONFIRMATORY,
    STATUS_CONFIRMATORY_INCOMPLETE,
    STATUS_EXPLORATORY,
    analysis_status,
    create_manifest,
    label_exploratory,
    load_manifest,
    manifest_hash,
    methods_section,
    replicates_sha256,
    run_manifest,
    validate_manifest,
)

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "examples", "manifests", "income_population_tracts.json")
N_TRACTS = 30


def synthetic_payload(table_code, seed=7):
    """Tract payload for B19013 (income) or B01003 (population); income rises with population."""
    rng = np.random.default_rng(seed)
    pop = rng.uniform(1000, 8000, N_TRACTS).round()
    income = 30000 + 4.0 * pop + rng.normal(0, 4000, N_TRACTS)
    code = {"B19013": "B19013_001", "B01003": "B01003_001"}[table_code]
    est = income if table_code == "B19013" else pop
    moe = est * (0.12 if table_code == "B19013" else 0.08)
    header = ["GEO_ID", "NAME", f"{code}E", f"{code}EA", f"{code}M", f"{code}MA", "state", "county", "tract"]
    rows = [header]
    for i in range(N_TRACTS):
        tract = f"{(i + 1) * 100:06d}"
        rows.append([f"1400000US36061{tract}", f"Tract {i + 1}", f"{est[i]:.0f}", None, f"{moe[i]:.0f}", None,
                     "36", "061", tract])
    return rows


class FakeClient(CensusAPIClient):
    def __init__(self, fail_tables=()):
        super().__init__(api_key="", cache=None)
        self.fail_tables = set(fail_tables)

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        if table_code in self.fail_tables and kwargs.get("geo_level") != "us":
            self.last_error = "HTTP 500"
            return None
        self.last_error = None
        return synthetic_payload(table_code)

    def fetch_variable_metadata(self, year, table_code, dataset=None):  # type: ignore[override]
        return {}


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.m = load_manifest(EXAMPLE)

    def test_example_is_valid_and_hash_stable(self):
        res = create_manifest(EXAMPLE)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["hash"], manifest_hash(self.m))
        reordered = json.loads(json.dumps(self.m, sort_keys=False))
        reordered = dict(reversed(list(reordered.items())))
        self.assertEqual(manifest_hash(reordered), res["hash"])
        changed = copy.deepcopy(self.m)
        changed["confidence_level"] = 0.95
        self.assertNotEqual(manifest_hash(changed), res["hash"])

    def test_unknown_variable_and_method_rejected(self):
        bad = copy.deepcopy(self.m)
        bad["variables"][0]["ref"] = "2019:B19013_999"
        bad["analyses"][0]["method"] = "random_forest"
        bad["authors"] = ["jdoe"]
        errs = validate_manifest(bad)
        self.assertTrue(any("B19013_999" in e for e in errs))
        self.assertTrue(any("random_forest" in e for e in errs))
        self.assertTrue(any("human:" in e for e in errs))

    def test_formula_variables_and_hypotheses_checked(self):
        bad = copy.deepcopy(self.m)
        bad["analyses"][0]["formula"] = "income ~ unknown_var"
        bad["hypotheses"][1]["statistic"] = "coefficient"
        bad["hypotheses"][0]["direction"] = "up"
        errs = validate_manifest(bad, resolve=False)
        self.assertTrue(any("unknown_var" in e for e in errs))
        self.assertTrue(any("not available from correlation_matrix" in e for e in errs))
        self.assertTrue(any("direction" in e for e in errs))

    def test_concept_id_needs_table(self):
        bad = copy.deepcopy(self.m)
        bad["variables"][1] = {"name": "pop", "ref": "c_0123456789ab"}
        self.assertTrue(any("needs 'table'" in e for e in validate_manifest(bad, resolve=False)))

    def test_create_writes_file(self):
        d = tempfile.mkdtemp()
        try:
            res = create_manifest(self.m, path=os.path.join(d, "m.json"))
            self.assertTrue(res["valid"])
            self.assertEqual(manifest_hash(load_manifest(res["path"])), res["hash"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestExploratory(unittest.TestCase):
    def test_label(self):
        r = label_exploratory({"x": 1}, manifest_hash_value="abc")
        self.assertEqual(analysis_status(r), STATUS_EXPLORATORY)
        self.assertEqual(r["related_manifest_hash"], "abc")
        with self.assertRaises(TypeError):
            label_exploratory(3)


class TestRunManifest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.m = load_manifest(EXAMPLE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_end_to_end(self):
        res = run_manifest(self.m, self.dir, client=FakeClient(), use_cache=False)
        h = manifest_hash(self.m)
        self.assertTrue(res["success"], res["deviations"])
        self.assertEqual(res["deviations"], [])
        self.assertEqual(len(res["frame"]), N_TRACTS)
        hyp = res["hypotheses"].set_index("id")
        self.assertEqual(hyp.loc["H1", "status"], "tested")
        self.assertGreater(hyp.loc["H1", "estimate"], 0)
        self.assertTrue(bool(hyp.loc["H1", "significant"]))
        self.assertLessEqual(hyp.loc["H1", "p_value"], hyp.loc["H1", "p_adjusted"])
        self.assertEqual(res["results"]["m1"].provenance["analysis_status"], STATUS_CONFIRMATORY)
        bundle = os.path.join(self.dir, "bundle")
        # hash in every bundle output
        for rel in res["bundle"]["files"]:
            if rel.endswith((".json", ".md", ".tex", ".csv")) and not rel.startswith("figures/") \
                    or rel.endswith(".provenance.json"):
                with open(os.path.join(bundle, rel), encoding="utf-8") as f:
                    self.assertIn(h, f.read(), rel)
        with open(os.path.join(bundle, "bundle.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["provenance"]["manifest_hash"], h)
        with open(os.path.join(bundle, "methods.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Variance Replicate Estimate", text)
        self.assertIn("Holm", text)
        self.assertIn("2015-2019", text)

    def test_failed_extraction_is_a_deviation(self):
        res = run_manifest(self.m, self.dir, client=FakeClient(fail_tables={"B01003"}), use_cache=False)
        self.assertFalse(res["success"])
        kinds = {d["kind"] for d in res["deviations"]}
        self.assertTrue(kinds & {"extraction_failed", "failed_calls", "analysis_failed", "analysis_not_run"})
        self.assertIn("hypothesis_not_tested", kinds)
        self.assertTrue((res["hypotheses"]["status"] == "not_tested").all())
        with open(os.path.join(self.dir, "bundle", "methods.md"), encoding="utf-8") as f:
            self.assertIn("Deviations from the manifest", f.read())

    def test_invalid_manifest_not_run(self):
        bad = copy.deepcopy(self.m)
        bad["analyses"][0]["method"] = "nope"
        res = run_manifest(bad, self.dir, client=FakeClient())
        self.assertFalse(res["success"])
        self.assertTrue(res["errors"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "manifest.json")))

    def _read(self, rel):
        with open(os.path.join(self.dir, "bundle", rel), encoding="utf-8") as f:
            return f.read()

    def test_status_confirmatory_only_when_clean(self):
        res = run_manifest(self.m, self.dir, client=FakeClient(), use_cache=False)
        self.assertEqual(res["analysis_status"], STATUS_CONFIRMATORY)
        self.assertEqual(json.loads(self._read("results/run.json"))["analysis_status"], STATUS_CONFIRMATORY)
        self.assertEqual(json.loads(self._read("bundle.json"))["provenance"]["analysis_status"], STATUS_CONFIRMATORY)
        self.assertIn("**Analysis status: confirmatory**.", self._read("methods.md"))

    def test_status_incomplete_with_blocking_deviations(self):
        res = run_manifest(self.m, self.dir, client=FakeClient(fail_tables={"B01003"}), use_cache=False)
        self.assertEqual(res["analysis_status"], STATUS_CONFIRMATORY_INCOMPLETE)
        run = json.loads(self._read("results/run.json"))
        self.assertEqual(run["analysis_status"], STATUS_CONFIRMATORY_INCOMPLETE)
        self.assertEqual(json.loads(self._read("bundle.json"))["provenance"]["analysis_status"],
                         STATUS_CONFIRMATORY_INCOMPLETE)
        methods = self._read("methods.md")
        self.assertIn("confirmatory_incomplete", methods)
        self.assertIn("pre-registered analysis did not run as registered; results are not confirmatory", methods)
        self.assertNotIn("**Analysis status: confirmatory**.", methods)

    def test_data_source_validation(self):
        for bad in ({"synthetic": "yes"}, {"description": "x"}, {"synthetic": True, "extra": 1},
                    {"synthetic": True, "generator": 3}, "synthetic"):
            m = copy.deepcopy(self.m)
            m["data_source"] = bad
            self.assertTrue(any("data_source" in e for e in validate_manifest(m, resolve=False)), bad)
        m = copy.deepcopy(self.m)
        m.pop("data_source", None)
        self.assertEqual(validate_manifest(m, resolve=False), [])

    def test_synthetic_source_citation_and_methods(self):
        # the example manifest declares no data_source: the label follows the observed source
        self.assertNotIn("data_source", self.m)
        m = copy.deepcopy(self.m)
        m["data_source"] = {"synthetic": True, "description": "generated tracts"}
        client = FakeClient()
        client.synthetic = True
        res = run_manifest(m, self.dir, client=client, use_cache=False)
        self.assertEqual(res["deviations"], [])
        bundle = json.loads(self._read("bundle.json"))
        methods = self._read("methods.md")
        for text in (bundle["citation"], methods):
            self.assertIn("SYNTHETIC", text)
            self.assertNotIn("Retrieved via the Census Data API", text)
        self.assertTrue(bundle["provenance"]["data_source"]["synthetic"])
        self.assertIsNone(bundle["provenance"]["replicates"])

    def test_synthetic_client_flag_overrides_real_manifest(self):
        m = copy.deepcopy(self.m)
        m.pop("data_source", None)
        client = FakeClient()
        client.synthetic = True
        run_manifest(m, self.dir, client=client, use_cache=False)
        self.assertNotIn("Retrieved via the Census Data API", self._read("methods.md"))
        self.assertIn("SYNTHETIC", json.loads(self._read("bundle.json"))["citation"])
        # a real-source manifest still cites the Census Data API
        self.assertIn("Retrieved via the Census Data API", methods_section(m))

    def test_caller_supplied_replicates_described_and_hashed(self):
        from census.simulation import make_replicates, replicate_long
        frame = run_manifest(self.m, os.path.join(self.dir, "probe"), client=FakeClient(), use_cache=False)["frame"]
        est = frame[["income", "pop"]].to_numpy(float)
        se = frame[["income_se", "pop_se"]].to_numpy(float)
        reps = replicate_long(list(frame["GEOID"]), ["income", "pop"], est,
                              make_replicates(est, se, np.random.default_rng(1)))
        reps["Year"] = 2019
        synthetic_client = FakeClient()
        synthetic_client.synthetic = True
        res = run_manifest(self.m, self.dir, client=synthetic_client, use_cache=False, replicates=reps)
        self.assertTrue(res["success"], res["deviations"])
        prov = json.loads(self._read("bundle.json"))["provenance"]
        digest = replicates_sha256(reps)
        self.assertEqual(prov["replicates"], {"source": "caller_supplied", "synthetic": True,
                                              "rows": len(reps), "sha256": digest})
        self.assertEqual(replicates_sha256(reps.iloc[::-1][list(reversed(reps.columns))]), digest)
        methods = self._read("methods.md")
        self.assertIn("synthetic caller-supplied replicate estimates", methods)
        self.assertIn(digest, methods)
        self.assertNotIn("80 ACS replicate estimates", methods)
        # not synthetic when the caller says so
        m = copy.deepcopy(self.m)
        m.pop("data_source", None)
        run_manifest(m, self.dir, client=FakeClient(), use_cache=False, replicates=reps)
        methods = self._read("methods.md")
        self.assertIn("the caller-supplied replicate estimates", methods)
        self.assertNotIn("synthetic caller-supplied", methods)

    def test_methods_section_without_results(self):
        text = methods_section(self.m)
        self.assertIn(manifest_hash(self.m), text)
        self.assertIn("B19013_001", text)


if __name__ == "__main__":
    unittest.main()
