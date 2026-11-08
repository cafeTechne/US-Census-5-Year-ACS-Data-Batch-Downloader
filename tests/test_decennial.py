# SPDX-License-Identifier: AGPL-3.0-or-later
"""2020 Decennial Census retrieval and uncertainty semantics (PLAN_ROUND_15 C1-C3).

Offline: canned API responses, the archived dec/pl and dec/dhc 2020 metadata
(metadata/dec/), and the frozen F11 fixtures.
"""

import json
import math
import os
import tempfile
import unittest

import pandas as pd

from census import decennial as D
from census import validation as V
from census import workflows
from census.analysis import analysis_frame
from census.api import (
    DATASETS,
    CensusAPIClient,
    build_geo_params,
    dataset_product,
    infer_dataset,
    require_same_product,
)
from census.metadata import archived_vintages, describe_variable, table_variables, verify_archive_file
from census.multitest import batch_z_test
from census.panel import classify_column, generate_codebook, measurement_type
from census.pipeline import Entity, ResearchOptions, build_entities, plan_extraction, replay_extraction, run_batch
from census.stats import acs_overlap_years, add_reliability_columns, calculate_cv, parse_variable_code, z_test

P1_STATES = [
    ["NAME", "GEO_ID", "NAME", "P1_001N", "P1_001NA", "state"],
    ["Michigan", "0400000US26", "Michigan", "10077331", None, "26"],
    ["Wyoming", "0400000US56", "Wyoming", "576851", None, "56"],
]
H1_BLOCKS = [
    ["NAME", "H1_001N", "H1_002N", "H1_003N", "state", "county", "tract", "block"],
    ["Block 1000", "3", "2", "1", "56", "001", "962700", "1000"],
    ["Block 1001", "0", "0", "0", "56", "001", "962700", "1001"],
]


class FakeClient(CensusAPIClient):
    def __init__(self):
        super().__init__(api_key="", cache=None)
        self.requests = []

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        self.requests.append(self.build_request(year=year, table_code=table_code, dataset=dataset, **kwargs))
        self.last_error = None
        return H1_BLOCKS if kwargs.get("geo_level") == "block" else P1_STATES


def _opts(**kw):
    return ResearchOptions(use_cache=False, **kw)


class TestDatasets(unittest.TestCase):
    def test_dataset_keys_and_products(self):
        self.assertEqual(DATASETS["dec/pl"], "dec/pl")
        self.assertEqual(DATASETS["dec/dhc"], "dec/dhc")
        self.assertEqual(dataset_product("dec/dhc"), "decennial")
        self.assertEqual(infer_dataset("P12", "decennial"), "dec/dhc")
        self.assertEqual(infer_dataset("P1", "dec/pl"), "dec/pl")
        self.assertEqual(infer_dataset("B01001"), "acs5")  # ACS default unchanged
        url = CensusAPIClient(api_key="").endpoint(2020, "dec/pl")
        self.assertEqual(url, "https://api.census.gov/data/2020/dec/pl")

    def test_example_tables_exist_in_archived_dhc(self):
        self.assertEqual(archived_vintages("dec/dhc"), [2020])
        self.assertTrue(verify_archive_file("dec/dhc", 2020))
        self.assertTrue(verify_archive_file("dec/pl", 2020))
        for table, first in (("P1", "P1_001N"), ("P12", "P12_002N"), ("H1", "H1_001N"), ("H3", "H3_002N")):
            variables, source = table_variables(table, "dec/dhc", 2020)
            self.assertEqual(source, "archive")
            self.assertIn(first, variables, table)
        self.assertEqual(table_variables("H3", "dec/dhc", 2020)[0]["H3_002N"]["label"], "Total:!!Occupied")

    def test_block_geo_params(self):
        self.assertEqual(build_geo_params("block", state_code="56", county_code="001"),
                         {"for": "block:*", "in": "state:56 county:001"})
        with self.assertRaises(ValueError):
            build_geo_params("block", state_code="26,56")
        ents = build_entities("block", "56", ["001", "3"])
        self.assertEqual([e.county_code for e in ents], ["001", "003"])
        with self.assertRaises(ValueError):
            build_entities("block", "all")

    def test_variable_codes(self):
        self.assertEqual(parse_variable_code("P12_002N"), ("P12_002", "N"))
        self.assertEqual(parse_variable_code("P12_002NA"), ("P12_002", "NA"))
        self.assertEqual(classify_column("P1_001N"), ("P1_001", "N", False))
        self.assertEqual(classify_column("P1_001NA")[1], "ANN")
        self.assertEqual(measurement_type("P1_001N"), "Count (not sample-based)")
        # ACS codes are unaffected
        self.assertEqual(parse_variable_code("B01001_001E"), ("B01001_001", "E"))

    def test_describe_variable_defaults_to_dhc(self):
        d = describe_variable("P12_002N")
        self.assertTrue(d["found"])
        self.assertEqual((d["dataset"], d["year"], d["table"]), ("dec/dhc", 2020, "P12"))
        self.assertEqual(d["uncertainty"], D.UNCERTAINTY_LABEL)
        self.assertEqual(describe_variable("P1_001N", dataset="dec/pl")["concept"], "RACE")


class TestPlanAndRun(unittest.TestCase):
    def test_plan_refusals(self):
        state = [Entity("WY", "state", "56")]
        cases = [
            (dict(entities=state, years=[2019, 2020], dataset="dec/dhc"), "VINTAGE_UNAVAILABLE"),
            (dict(entities=[Entity("Z", "zcta", zcta_code="82001")], years=[2020], dataset="dec/dhc"),
             "INVALID_GEO_LEVEL"),
            (dict(entities=build_entities("block", "56", ["001"]), years=[2020], dataset="dec/dhc",
                  table="P12"), "INVALID_GEO_LEVEL"),
            (dict(entities=build_entities("block", "56", ["001"]), years=[2020], dataset="acs5",
                  table="B01001"), "INVALID_GEO_LEVEL"),
            (dict(entities=state, years=[2020], dataset="dec/dhc", product="1-year"), "INVALID_DATASET"),
            (dict(entities=state, years=[2020], dataset="acs5", product="decennial"), "INVALID_DATASET"),
            (dict(entities=state, years=[2020], dataset="dec/dhc", options=_opts(inflation_base_year=2023)),
             "INVALID_INPUT"),
        ]
        for kw, code in cases:
            with self.subTest(kw=kw):
                plan = plan_extraction(kw.pop("table", "P1"), kw.pop("entities"), kw.pop("years"),
                                       options=kw.pop("options", _opts()), client=FakeClient(), **kw)
                self.assertFalse(plan["success"])
                self.assertEqual(plan["error_code"], code)

    def test_plan_product_decennial(self):
        plan = plan_extraction("P1", [Entity("WY", "state", "56")], [2020],
                               options=_opts(compute_reliability=True), client=FakeClient(), product="decennial")
        self.assertTrue(plan["success"], plan.get("message"))
        self.assertEqual((plan["dataset"], plan["product"]), ("dec/dhc", "decennial"))
        self.assertIn(D.DISCLOSURE_AVOIDANCE_ADVISORY, plan["advisories"])
        self.assertTrue(any("compute_reliability" in a for a in plan["advisories"]))
        self.assertFalse(plan["uncertainty"]["sample_based"])
        self.assertIsNone(plan["uncertainty"]["sampling_moe"])
        self.assertEqual(plan["errata"], [])

    def test_run_batch_outputs_never_carry_zero_moe(self):
        with tempfile.TemporaryDirectory() as d:
            res = run_batch("P1", [Entity("MI_WY", "state", "26,56")], [2020], d, "panel",
                            _opts(include_codebook=True, compute_reliability=True), "dec/dhc", FakeClient())
            self.assertTrue(res["success"], res.get("message"))
            panel = pd.read_csv(res["panel_path"], dtype={"geoid": str})
            self.assertEqual(list(panel.columns), ["geoid", "year", "name", "total_count"])
            self.assertEqual(panel["total_count"].tolist(), [10077331, 576851])
            cb = pd.read_csv(res["codebook_path"])
            row = cb[cb["Variable_Code"] == "P1_001N"].iloc[0]
            self.assertEqual(row["Measurement_Type"], "Count (not sample-based)")
            self.assertIn("MOE undefined", row["Uncertainty"])
            self.assertIn("disclosure-avoidance", row["Uncertainty"])
            self.assertIn("Census year", cb[cb["Variable_Code"] == "Year"].iloc[0]["Full_Description"])
            with open(res["metadata_path"], encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["product"], "decennial")
            self.assertIsNone(meta["moe_confidence_level"])
            self.assertEqual(meta["uncertainty"]["sampling_moe_status"], D.UNCERTAINTY_LABEL)
            self.assertIn(D.INVARIANTS_URL, meta["uncertainty"]["sources"])
            replay = replay_extraction(res["metadata_path"], client=FakeClient())
            self.assertTrue(replay["success"] and replay["verified"], replay)

    def test_run_batch_blocks(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as d:
            res = run_batch("H1", build_entities("block", "56", ["001"]), [2020], d, "panel", _opts(),
                            "dec/pl", client)
            self.assertTrue(res["success"], res.get("message"))
            panel = pd.read_csv(res["panel_path"], dtype={"geoid": str})
            self.assertEqual(panel["geoid"].tolist(), ["560019627001000", "560019627001001"])
            self.assertIn(D.BLOCK_ADVISORY, res["advisories"])
        self.assertEqual(client.requests[0][1]["for"], "block:*")
        self.assertEqual(client.requests[0][1]["in"], "state:56 county:001")

    def test_reliability_columns_not_added(self):
        df = pd.DataFrame({"P1_001N": [10.0], "P1_001NA": [None]})
        self.assertEqual(list(add_reliability_columns(df).columns), ["P1_001N", "P1_001NA"])

    def test_codebook_uncertainty_column_only_for_decennial(self):
        meta = {"P1_001N": {"label": "Total", "concept": "TOTAL POPULATION"}}
        acs = generate_codebook(["B01001_001E"], {})
        self.assertNotIn("Uncertainty", acs.columns)
        dec = generate_codebook(["GEOID", "P1_001N"], meta, decennial=True)
        self.assertEqual(dec["Uncertainty"].iloc[0], "")
        self.assertTrue(dec["Uncertainty"].iloc[1].startswith(D.UNCERTAINTY_LABEL))


class TestSignificanceSemantics(unittest.TestCase):
    def test_decennial_only_tests_refused(self):
        for call in (
            lambda: z_test(100, 0, 90, 0, product="decennial"),
            lambda: z_test(100, None, 90, None, product1="dec/pl", product2="dec/dhc"),
            lambda: calculate_cv(100, 0, product="decennial"),
            lambda: acs_overlap_years(2020, 2020, "decennial"),
            lambda: require_same_product("dec/dhc"),
            lambda: batch_z_test(pd.DataFrame({"a": [1.0], "b": [0.0], "c": [2.0], "d": [0.0]}),
                                 "a", "b", "c", "d", product="decennial"),
        ):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn("not sample-based", str(cm.exception))

    def test_acs_vs_decennial_reference(self):
        r = z_test(1050, 30, 1000, None, product1="5-year", product2="decennial")
        self.assertEqual(r["comparison"], "acs_vs_decennial_reference")
        self.assertIn(D.MIXED_NOTE, r["notes"])
        self.assertAlmostEqual(r["se_difference"], 30 / 1.645, places=6)
        self.assertTrue(r["verdict"].startswith("ACS estimate vs decennial count"))
        with self.assertRaises(ValueError):  # a decennial MOE is not accepted
            z_test(1050, 30, 1000, 12, product1="5-year", product2="decennial")
        with self.assertRaises(ValueError):  # no overlap between a survey and a census
            z_test(1050, 30, 1000, None, overlap_years=3, product1="5-year", product2="decennial")
        acs = z_test(1050, 30, 1000, 30, product="5-year")
        self.assertEqual(acs["comparison"], "acs_vs_acs")

    def test_derive_estimate_decennial_point_only(self):
        out = workflows.derive_estimate("proportion", [19137, 37066], None, product="decennial")
        self.assertTrue(out["success"])
        self.assertAlmostEqual(out["estimate"], 19137 / 37066)
        self.assertIsNone(out["moe"])
        self.assertIsNone(out["se"])
        self.assertIsNone(out["cv_percent"])
        self.assertTrue(all(v is None for k, v in out["components"].items() if "moe" in k))
        self.assertEqual(out["uncertainty"], D.UNCERTAINTY_LABEL)
        bad = workflows.derive_estimate("sum", [1, 2], [1, 1], product="decennial")
        self.assertFalse(bad["success"])

    def test_batch_significance_refuses_decennial(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.csv")
            pd.DataFrame({"GEOID": ["1"], "a": [1.0], "b": [0.0], "c": [2.0], "d": [0.0]}).to_csv(path, index=False)
            out = workflows.batch_significance(path, "a", "b", "c", "d", product="decennial")
        self.assertFalse(out["success"])
        self.assertIn("not sample-based", out["error"])

    def test_mcp_tools(self):
        import mcp_server

        self.assertFalse(mcp_server.calculate_cv(100, 0, product="decennial")["success"])
        self.assertFalse(mcp_server.calculate_significance(1, 0, 2, 0, product="decennial")["success"])
        r = mcp_server.calculate_significance(1050, 30, 1000, 0, product="5-year", product2="decennial",
                                              year1=2020, year2=2020)
        self.assertEqual(r["comparison"], "acs_vs_decennial_reference")
        self.assertEqual(r["overlap_years"], 0)

    def test_analysis_frame_counts_have_undefined_se(self):
        df = pd.DataFrame({"GEOID": ["56001", "56003"], "Year": [2020, 2020],
                           "total_count": [37066, 11521], "total_male_count": [19137, 5865]})
        f = analysis_frame(df, variables=["total_count", "total_male_count"])
        self.assertTrue(f["total_count_se"].isna().all())
        self.assertTrue(f["total_count_cv"].isna().all())
        self.assertEqual(set(f["total_count_method"]), {"not_sample_based"})
        self.assertEqual(set(f["release"]), {"dec_2020"})
        mixed = df.assign(B01003_001E=[38000, 11800], B01003_001M=[0, 0])
        g = analysis_frame(mixed, variables=["total_count", "B01003_001"])
        self.assertEqual(set(g["release"]), {"acs5_2020+dec_2020"})
        self.assertIn(D.MIXED_NOTE, g.attrs["notes"])


class _Tampered(V.CensusSource):
    """Offline source that changes one block's housing-unit count in P.L. 94-171."""

    def get_json(self, endpoint, params):
        payload = super().get_json(endpoint, params)
        if endpoint.endswith("/dec/pl") and params.get("for") == "block:*" and "county:001" in params.get("in", ""):
            payload = [list(r) for r in payload]
            payload[1][0] = str(int(payload[1][0]) + 1)
        return payload


class TestF11(unittest.TestCase):
    def test_offline_passes(self):
        r = V.run_battery("F11")[0]
        self.assertEqual(r.status, V.STATUS_PASSED, f"{r.notes} {r.failures[:3]}")
        self.assertEqual(r.n_failed, 0)
        parts = r.details["parts"]
        for part in ("a_state_population", "b_block_pl_vs_dhc", "b_blocks_to_tracts", "b_blocks_to_county",
                     "c_county_dhc_vs_pl", "c_tract_dhc_vs_pl"):
            self.assertGreater(parts[part]["compared"], 0, part)
        self.assertEqual(r.details["published_state_population"], {"26": 10077331, "36": 20201249, "56": 576851})

    def test_detects_a_changed_block(self):
        r = V.run_battery("F11", source=_Tampered(live=False))[0]
        self.assertEqual(r.status, V.STATUS_FAILED)
        failed = {p for p, s in r.details["parts"].items() if s["failed"]}
        self.assertEqual(failed, {"b_block_pl_vs_dhc", "b_blocks_to_tracts", "b_blocks_to_county"})
        self.assertTrue(math.isfinite(r.max_abs_error) and r.max_abs_error == 1)


if __name__ == "__main__":
    unittest.main()
