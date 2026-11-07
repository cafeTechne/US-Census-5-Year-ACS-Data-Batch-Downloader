# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACS 1-year estimates: datasets, product routing, geography probes, 2020, overlap, VRE."""

import json
import os
import tempfile
import unittest

from census import errors
from census.api import (
    DATASETS,
    MIXED_PRODUCTS_MESSAGE,
    CensusAPIClient,
    dataset_for_product,
    dataset_product,
    infer_dataset,
    normalize_product,
    require_same_product,
)
from census.metadata import archived_vintages, table_variables, verify_archive_file
from census.pipeline import Entity, ResearchOptions, plan_extraction, run_batch
from census.replicate import vre_availability, vre_years
from census.stats import acs_overlap_years, overlap_factor, z_test

YEARS = [2022, 2023]


def county_payload(year, county="001"):
    header = ["NAME", "B19013_001E", "B19013_001M", "state", "county"]
    return [header, [f"County {county}", "100000", "2000", "06", county]]


class FakeClient(CensusAPIClient):
    """Canned 1-year responses: CA publishes counties 001 and 013 only."""

    published = {"001", "013"}

    def __init__(self, listing_fails=False):
        super().__init__(api_key="", cache=None)
        self.calls = []
        self.listing_calls = []
        self.listing_fails = listing_fails

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        self.calls.append((year, table_code, dataset, kwargs))
        self.last_error = None
        if kwargs.get("geo_level") == "us":
            return [["NAME", "B19013_001E", "B19013_001M", "us"], ["US", "75000", "200", "1"]]
        return county_payload(year, kwargs.get("county_code") or "001")

    def list_geographies(self, year, geo_level, state_code=None, county_code=None, dataset="acs5"):  # type: ignore[override]
        self.listing_calls.append((year, geo_level, state_code, dataset))
        if self.listing_fails:
            return []
        return [{"NAME": c, "state": "06", "county": c} for c in sorted(self.published)]


def counties(*codes):
    return [Entity(f"County {c}", "county", state_code="06", county_code=c) for c in codes]


class TestDatasets(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(DATASETS["acs1"], "acs/acs1")
        self.assertEqual(DATASETS["acs1/profile"], "acs/acs1/profile")
        self.assertEqual(DATASETS["acs1/subject"], "acs/acs1/subject")
        self.assertEqual(DATASETS["acs1/cprofile"], "acs/acs1/cprofile")
        self.assertEqual(
            CensusAPIClient(api_key="").endpoint(2023, "acs1/subject"),
            "https://api.census.gov/data/2023/acs/acs1/subject",
        )

    def test_infer_and_map(self):
        self.assertEqual(infer_dataset("B19013"), "acs5")
        self.assertEqual(infer_dataset("DP03"), "profile")
        self.assertEqual(infer_dataset("B19013", "1-year"), "acs1")
        self.assertEqual(infer_dataset("S1701", "1-year"), "acs1/subject")
        self.assertEqual(infer_dataset("CP03", "1-year"), "acs1/cprofile")
        self.assertEqual(infer_dataset("DP05", "acs1"), "acs1/profile")
        self.assertEqual(dataset_product("acs1/profile"), "1-year")
        self.assertEqual(dataset_product("subject"), "5-year")
        self.assertEqual(dataset_for_product("profile", "1-year"), "acs1/profile")
        self.assertEqual(dataset_for_product("acs1", "5-year"), "acs5")
        with self.assertRaises(ValueError):
            normalize_product("3-year")

    def test_require_same_product(self):
        self.assertEqual(require_same_product("acs1", "1-year"), "1-year")
        with self.assertRaises(ValueError) as ctx:
            require_same_product("acs1", "acs5")
        self.assertIn("comparing-acs-data", str(ctx.exception))


class TestArchive(unittest.TestCase):
    def test_vintages_and_checksums(self):
        years = archived_vintages("acs1")
        if not years:
            self.skipTest("1-year metadata archive not present")
        self.assertIn(2005, years)
        self.assertIn(2024, years)
        self.assertNotIn(2020, years)  # no 2020 1-year release in the API
        self.assertNotIn(2020, archived_vintages("acs1/profile"))
        self.assertEqual(min(archived_vintages("acs1/subject")), 2010)
        for ds in ("acs1", "acs1/profile", "acs1/subject", "acs1/cprofile"):
            self.assertTrue(verify_archive_file(ds, 2023), ds)
        variables, source = table_variables("B19013", "acs1", 2023)
        self.assertEqual(source, "archive")
        self.assertIn("B19013_001E", variables)


class TestPlan(unittest.TestCase):
    def plan(self, entities=None, years=YEARS, **kw):
        kw.setdefault("variables", ["B19013_001E"])
        return plan_extraction("B19013", entities or counties("001"), years, client=FakeClient(), **kw)

    def test_default_is_5_year(self):
        plan = self.plan()
        self.assertTrue(plan["success"], plan.get("error"))
        self.assertEqual((plan["dataset"], plan["product"]), ("acs5", "5-year"))
        self.assertIsNone(plan["geography_availability"])

    def test_product_routes_dataset(self):
        plan = self.plan(product="1-year")
        self.assertTrue(plan["success"], plan.get("error"))
        self.assertEqual((plan["dataset"], plan["product"]), ("acs1", "1-year"))
        self.assertTrue(any("65,000" in a for a in plan["advisories"]))
        plan = self.plan(options=ResearchOptions(product="1-year"), dataset="acs5")
        self.assertEqual(plan["dataset"], "acs1")
        plan = self.plan(dataset="acs1")  # explicit 1-year dataset implies 1-year
        self.assertEqual(plan["product"], "1-year")

    def test_mixed_product_refused(self):
        plan = self.plan(dataset="acs1", product="5-year")
        self.assertFalse(plan["success"])
        self.assertEqual(plan["error_code"], errors.INVALID_DATASET)
        self.assertIn(MIXED_PRODUCTS_MESSAGE, plan["error"])

    def test_small_geographies_refused(self):
        for level in ("tract", "block group", "zcta"):
            plan = self.plan([Entity("x", level, state_code="06", county_code="001")], product="1-year")
            self.assertEqual(plan["error_code"], errors.INVALID_GEO_LEVEL, level)

    def test_unavailable_areas_reported_and_skipped(self):
        plan = self.plan(counties("001", "003"), product="1-year")
        self.assertTrue(plan["success"], plan.get("error"))
        unavailable = plan["geography_availability"]["unavailable"]
        self.assertEqual([u["entity"] for u in unavailable], ["County 003"])
        self.assertEqual(unavailable[0]["missing_codes_by_year"], {"2022": ["003"], "2023": ["003"]})
        self.assertEqual(plan["api_calls"], 2)  # County 003 is skipped in both years

    def test_multi_code_target_partially_published(self):
        ent = Entity("CA", "county", state_code="06", county_code="001,003")
        plan = self.plan([ent], product="1-year")
        self.assertEqual(plan["api_calls"], 2)
        self.assertEqual(plan["geography_availability"]["unavailable"][0]["missing_codes_by_year"]["2022"], ["003"])

    def test_all_unavailable_errors(self):
        plan = self.plan(counties("003"), product="1-year")
        self.assertEqual(plan["error_code"], errors.INVALID_GEO_LEVEL)
        self.assertIn("geography_availability", plan)

    def test_wildcard_and_offline(self):
        ent = Entity("All CA", "county", state_code="06")
        client = FakeClient()
        plan = plan_extraction("B19013", [ent], YEARS, client=client, product="1-year", variables=["B19013_001E"])
        self.assertTrue(plan["success"])
        self.assertEqual(client.listing_calls, [])  # wildcard: API returns only published areas
        offline = FakeClient(listing_fails=True)
        plan = plan_extraction("B19013", counties("003"), YEARS, client=offline, product="1-year",
                               variables=["B19013_001E"])
        self.assertTrue(plan["success"])
        self.assertEqual(plan["geography_availability"]["unchecked_years"], YEARS)

    def test_2020_unavailable_by_default(self):
        plan = self.plan(years=[2019, 2020, 2021], product="1-year")
        self.assertEqual(plan["error_code"], errors.VINTAGE_UNAVAILABLE)
        self.assertIn("experimental", plan["error"])
        self.assertIn("experimental-data", plan["error"])

    def test_2020_opt_in_excludes_with_advisory(self):
        opts = ResearchOptions(product="1-year", allow_experimental_2020=True)
        plan = self.plan(years=[2019, 2020, 2021], options=opts)
        self.assertTrue(plan["success"], plan.get("error"))
        self.assertEqual(plan["years"], [2019, 2021])
        self.assertEqual(plan["excluded_years"], [2020])
        self.assertTrue(any(a.startswith("EXPERIMENTAL 2020") for a in plan["advisories"]))
        only = self.plan(years=[2020], options=opts)
        self.assertEqual(only["error_code"], errors.VINTAGE_UNAVAILABLE)

    def test_2020_fine_for_5_year(self):
        self.assertTrue(self.plan(years=[2019, 2020])["success"])

    def test_concordance_across_1_year_vintages(self):
        plan = self.plan(years=[2019, 2021, 2023], product="1-year",
                         options=ResearchOptions(continuity_check=False))
        if not archived_vintages("acs1"):
            self.skipTest("1-year metadata archive not present")
        self.assertTrue(plan["success"], plan.get("error"))
        self.assertFalse(plan["would_block"])
        self.assertEqual(plan["concordance_summary"]["years"], [2019, 2021, 2023])


class TestRunBatch(unittest.TestCase):
    def test_run_records_product_and_skips(self):
        if not archived_vintages("acs1"):
            self.skipTest("1-year metadata archive not present")
        client = FakeClient()
        with tempfile.TemporaryDirectory() as out:
            result = run_batch("B19013", counties("001", "003"), YEARS, out, output_format="panel",
                               client=client, variables=["B19013_001E"], product="1-year")
            self.assertTrue(result["success"], result.get("error"))
            self.assertEqual(result["dataset"], "acs1")
            self.assertEqual(result["failed_calls"], [])
            data_calls = [c for c in client.calls if c[3].get("geo_level") == "county"]
            self.assertEqual({c[3]["county_code"] for c in data_calls}, {"001"})
            self.assertEqual({c[2] for c in data_calls}, {"acs1"})
            with open(result["metadata_path"], encoding="utf-8") as f:
                meta = json.load(f)
        self.assertEqual(meta["product"], "1-year")
        self.assertEqual(meta["options"]["product"], "1-year")
        self.assertEqual(meta["endpoint"], "https://api.census.gov/data/{year}/acs/acs1")
        self.assertEqual(meta["geography_availability"]["unavailable"][0]["entity"], "County 003")


class TestOverlap(unittest.TestCase):
    def test_1_year_never_overlaps(self):
        self.assertEqual(acs_overlap_years(2022, 2023, "1-year"), 0)
        self.assertEqual(acs_overlap_years(2019, 2021, "acs1"), 0)
        self.assertEqual(acs_overlap_years(2023, 2023, "1-year"), 1)  # same period
        self.assertEqual(overlap_factor(1, 1), 1.0)
        self.assertEqual(acs_overlap_years(2022, 2023), 4)  # 5-year unchanged

    def test_mixed_refused(self):
        with self.assertRaises(ValueError) as ctx:
            acs_overlap_years(2022, 2023, "1-year", "5-year")
        self.assertIn("Only compare 1-year estimates with 1-year estimates", str(ctx.exception))
        with self.assertRaises(ValueError):
            z_test(100, 10, 120, 10, product1="acs1", product2="acs5")

    def test_z_test_1_year(self):
        r = z_test(100, 16.45, 130, 16.45, product1="1-year", product2="1-year",
                   overlap_years=acs_overlap_years(2023, 2023, "1-year"))
        self.assertAlmostEqual(r["se_difference"], 2 ** 0.5 * 10, places=6)
        plain = z_test(100, 16.45, 130, 16.45)
        self.assertEqual(r["se_difference"], plain["se_difference"])


class TestReplicates(unittest.TestCase):
    def test_no_1_year_vre(self):
        self.assertEqual(vre_years("1-year"), [])
        self.assertEqual(vre_years(), vre_years("5-year"))
        out = vre_availability("B19013", 2023, "county", product="1-year")
        self.assertFalse(out["available"])
        self.assertIn("5-year", out["reason"])


if __name__ == "__main__":
    unittest.main()
