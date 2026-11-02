"""Integration tests for census.pipeline using a fake (offline) API client."""

import json
import os
import shutil
import tempfile
import unittest

import pandas as pd

from census.api import CensusAPIClient
from census.inflation import CPI_U_ANNUAL
from census.pipeline import (
    ZCTA_CHUNK_SIZE,
    ResearchOptions,
    build_entities,
    prepare_frame,
    run_batch,
)

TRACT_HEADER = [
    "GEO_ID", "NAME", "B19013_001E", "B19013_001EA", "B19013_001M",
    "B19013_001MA", "state", "county", "tract",
]


def tract_payload(income="50000"):
    return [
        TRACT_HEADER,
        ["1400000US36061000100", "Tract 1", income, None, "4935", None, "36", "061", "000100"],
        ["1400000US36061000200", "Tract 2", "-666666666", None, "-222222222", None, "36", "061", "000200"],
    ]


class FakeClient(CensusAPIClient):
    """Returns canned payloads; records requests; fails for configured years."""

    def __init__(self, fail_years=()):
        super().__init__(api_key="", cache=None)
        self.calls = []
        self.fail_years = set(fail_years)

    def fetch(self, year, table_code, dataset=None, **kwargs):  # type: ignore[override]
        self.calls.append((year, table_code, dataset, kwargs))
        if year in self.fail_years and kwargs.get("geo_level") != "us":
            self.last_error = "HTTP 404"
            return None
        self.last_error = None
        return tract_payload()

    def fetch_variable_metadata(self, year, table_code, dataset=None):  # type: ignore[override]
        return {}


class TestBuildEntities(unittest.TestCase):
    def test_place_requires_codes(self):
        with self.assertRaises(ValueError):
            build_entities("place", "36")
        ents = build_entities("place", "36", ["51000"], {"51000": "New York city"})
        self.assertEqual(ents[0].name, "New York city")
        self.assertEqual(ents[0].request_kwargs()["place_code"], "51000")

    def test_state_required(self):
        with self.assertRaises(ValueError):
            build_entities("county")
        with self.assertRaises(ValueError):
            build_entities("bogus", "36")

    def test_tract_one_request_per_county(self):
        ents = build_entities("tract", "48", ["453", "201"], {"453": "Travis County"})
        self.assertEqual(len(ents), 2)
        self.assertEqual(ents[0].county_code, "453")
        self.assertEqual(ents[0].name, "Travis County_Tracts")
        all_tracts = build_entities("tract", "48")
        self.assertEqual(all_tracts[0].county_code, "*")

    def test_zcta_chunking_shares_group(self):
        zips = [f"{i:05d}" for i in range(ZCTA_CHUNK_SIZE + 5)]
        ents = build_entities("zcta", "36", zips)
        self.assertEqual(len(ents), 2)
        self.assertEqual(len(ents[0].zcta_code.split(",")), ZCTA_CHUNK_SIZE)
        self.assertEqual({e.group for e in ents}, {"New York_ZCTAs"})
        self.assertNotEqual(ents[0].name, ents[1].name)

    def test_zcta_defaults_to_state_zips(self):
        ents = build_entities("zcta", "11")
        self.assertIn("20001", ",".join(e.zcta_code for e in ents))

    def test_national_levels(self):
        self.assertEqual(build_entities("us")[0].geo_level, "us")
        msa = build_entities("msa", codes=["12420", "35620"])
        self.assertEqual(msa[0].msa_code, "12420,35620")
        self.assertEqual(build_entities("msa")[0].msa_code, "*")


class TestPrepareFrame(unittest.TestCase):
    def test_full_research_processing(self):
        meta = {"B19013_001E": {"label": "Median household income (dollars)", "concept": ""}}
        opts = ResearchOptions(
            compute_reliability=True, inflation_base_year=2022, price_index="cpi-u"
        )
        df = prepare_frame(tract_payload(), 2010, opts, meta, "tract")
        factor = CPI_U_ANNUAL[2022] / CPI_U_ANNUAL[2010]
        self.assertEqual(df["GEOID"].tolist(), ["36061000100", "36061000200"])
        self.assertAlmostEqual(df["B19013_001E"].iloc[0], 50000 * factor)
        self.assertAlmostEqual(df["B19013_001M"].iloc[0], 4935 * factor)
        self.assertTrue(pd.isna(df["B19013_001E"].iloc[1]))
        # CV is scale invariant: 3000 / 50000
        self.assertAlmostEqual(df["B19013_001_CV"].iloc[0], 6.0)

    def test_inflation_never_scales_sentinels(self):
        meta = {"B19013_001E": {"label": "income (dollars)", "concept": ""}}
        opts = ResearchOptions(clean_sentinels=False, inflation_base_year=2022, price_index="cpi-u")
        df = prepare_frame(tract_payload(), 2010, opts, meta, "tract")
        self.assertTrue(pd.isna(df["B19013_001E"].iloc[1]))


class TestRunBatch(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.entities = build_entities("tract", "36", ["061"], {"061": "New York County"})

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _run(self, fmt, client=None, **opts):
        return run_batch(
            "B19013",
            self.entities,
            [2018, 2019, 2020],
            self.test_dir,
            fmt,
            ResearchOptions(use_cache=False, **opts),
            client=client or FakeClient(),
        )

    def test_panel_csv_with_codebook(self):
        res = self._run("panel", compute_reliability=True, include_codebook=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["successful_calls"], 3)
        panel = pd.read_csv(res["panel_path"], dtype={"geoid": str})
        self.assertEqual(len(panel), 6)  # 2 tracts x 3 years
        self.assertFalse(panel.duplicated(["geoid", "year"]).any())
        self.assertIn("median_household_income_in_the_past_12_months_est", panel.columns)
        self.assertIn("median_household_income_in_the_past_12_months_cv", panel.columns)

        codebook = pd.read_csv(res["codebook_path"])
        self.assertEqual(set(codebook["Variable_Slug"]), set(panel.columns))
        self.assertEqual(codebook.set_index("Variable_Code").loc["B19013_001E", "Universe"], "Households")

        # Boundary advisory recorded in result + metadata JSON
        self.assertTrue(any("2019/2020" in a for a in res["advisories"]))
        with open(res["metadata_path"], encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["years"], [2018, 2019, 2020])
        self.assertEqual(meta["advisories"], res["advisories"])

    def test_parquet(self):
        res = self._run("parquet")
        self.assertTrue(res["panel_path"].endswith(".parquet"))
        df = pd.read_parquet(res["panel_path"])
        self.assertEqual(len(df), 6)
        self.assertEqual(df["year"].dtype, "int64")

    def test_legacy_formats(self):
        res = self._run("both", include_codebook=True)
        folder = res["entities"][0]["folder"]
        wide = pd.read_csv(os.path.join(folder, "compiled_unmelted_data.csv"), dtype={"GEOID": str})
        self.assertEqual(len(wide), 6)
        self.assertIn("GEOID", wide.columns)
        self.assertTrue(any(c.startswith("Margin of Error") for c in wide.columns))
        self.assertTrue(os.path.exists(res["codebook_path"]))

    def test_inflation_applied_to_panel(self):
        res = self._run("panel", inflation_base_year=2020, price_index="cpi-u")
        panel = pd.read_csv(res["panel_path"], dtype={"geoid": str})
        col = "median_household_income_in_the_past_12_months_est"
        values = panel[panel["geoid"] == "36061000100"].set_index("year")[col]
        self.assertAlmostEqual(values[2020], 50000)
        self.assertAlmostEqual(values[2018], 50000 * CPI_U_ANNUAL[2020] / CPI_U_ANNUAL[2018], places=3)

    def test_failed_calls_reported(self):
        res = self._run("panel", client=FakeClient(fail_years={2019}))
        self.assertEqual(res["successful_calls"], 2)
        self.assertEqual(res["failed_calls"], [{"entity": "New York County_Tracts", "year": 2019, "error": "HTTP 404"}])
        self.assertEqual(res["entities"][0]["years_saved"], [2018, 2020])

    def test_validation_errors(self):
        self.assertFalse(self._run("pdf")["success"])
        bad_cpi = self._run("panel", inflation_base_year=1950)
        self.assertFalse(bad_cpi["success"])
        self.assertIn("CPI-U", bad_cpi["error"])

    def test_dataset_inferred_for_profiles(self):
        client = FakeClient()
        run_batch(
            "DP03",
            build_entities("state", "36"),
            [2022],
            self.test_dir,
            "panel",
            ResearchOptions(use_cache=False),
            client=client,
        )
        self.assertEqual(client.calls[0][2], "profile")


if __name__ == "__main__":
    unittest.main()
