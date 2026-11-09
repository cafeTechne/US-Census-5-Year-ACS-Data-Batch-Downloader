# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for census.replicate against frozen Census VRE fixtures.

Fixtures (tests/fixtures/vre/, each with a .json sidecar naming the source URL
and the SHA-256 of the original download):

* B19001_040_2023_subset.csv.zip - 2019-2023 release, states 01 10 11 44 72
* B19001_050_2014_subset.csv.gz  - 2010-2014 release (lowercase header,
  pre-2020 GEOID prefix, Latin-1 names), counties 05117 35013 72011
* B01001_050_2018_subset.csv.gz  - 2014-2018 release, counties 48017 48087
  48301 (controlled estimates and zero counts)
"""

import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import unittest
import warnings
import zipfile

import numpy as np
import pandas as pd

from census import replicate as rp
from census.geo import build_geoid
from census.variance import REPLICATE_COLUMNS, Z90

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "vre")
F2023 = os.path.join(FIXTURES, "B19001_040_2023_subset.csv.zip")
F2014 = os.path.join(FIXTURES, "B19001_050_2014_subset.csv.gz")
F2018 = os.path.join(FIXTURES, "B01001_050_2018_subset.csv.gz")
ALL_FIXTURES = (F2023, F2014, F2018)
B19001_PARTS = [f"B19001_{i:03d}" for i in range(2, 18)]


def _frame(path):
    return rp.read_vre_csv(path)


def _synthetic(rows):
    """Replicate frame from (GEOID, variable, estimate, moe, replicates[80])."""
    records = []
    for geoid, var, est, moe, reps in rows:
        rec = {"GEOID": geoid, "NAME": f"Area {geoid}", "variable": var, "estimate": est, "moe": moe}
        rec.update(dict(zip(REPLICATE_COLUMNS, reps)))
        rec["controlled"] = False
        records.append(rec)
    return pd.DataFrame(records)


def _manual_moe(est, reps):
    reps = np.asarray(reps, float)
    return Z90 * math.sqrt(4.0 / 80.0 * float(np.sum((reps - est) ** 2)))


class TestRegistry(unittest.TestCase):
    def test_releases_and_table_counts(self):
        self.assertEqual(rp.vre_years(), list(range(2014, 2025)))
        counts = {y: len(rp.vre_tables(y)) for y in rp.vre_years()}
        # 2024 documentation: "augmented versions of 134 detailed tables".
        self.assertEqual(counts[2024], 134)
        self.assertEqual(counts[2014], 107)
        for tables in (rp.vre_tables(y) for y in rp.vre_years()):
            self.assertEqual(len(tables), len(set(tables)))
            self.assertIn("B19001", tables)
            self.assertIn("B17001", tables)
            self.assertNotIn("B19013", tables)  # medians are not published as VREs

    def test_layout_facts(self):
        old, new = rp.VRE_LAYOUTS[2014], rp.VRE_LAYOUTS[2024]
        self.assertEqual(old["file_ext"], ".csv.gz")
        self.assertEqual(rp.VRE_LAYOUTS[2018]["file_ext"], ".csv.gz")
        self.assertEqual(rp.VRE_LAYOUTS[2019]["file_ext"], ".csv.zip")
        self.assertEqual(new["compression"], "zip")
        self.assertEqual(old["estimate_column"], "estimate")
        self.assertEqual(rp.VRE_LAYOUTS[2018]["estimate_column"], "ESTIMATE")
        self.assertEqual(rp.VRE_LAYOUTS[2019]["geoid_prefix_example"], "05000US")
        self.assertEqual(rp.VRE_LAYOUTS[2020]["geoid_prefix_example"], "0500000US")
        self.assertIsNone(rp.VRE_LAYOUTS[2017]["table_list_url"])
        self.assertTrue(rp.VRE_LAYOUTS[2018]["table_list_url"].endswith("VRE_TABLE_LIST_2018.csv"))
        self.assertTrue(
            old["documentation_url"].endswith("2010_2014_Variance_Replicate_Tables_Documentation.pdf")
        )
        self.assertTrue(
            new["documentation_url"].endswith("2020-2024_Variance_Replicate_Table_Documentation.pdf")
        )
        for layout in rp.VRE_LAYOUTS.values():
            self.assertTrue(layout["data_dir"].startswith(rp.VRE_BASE_URL))
            self.assertEqual(len(layout["table_geo_list_sha256"]), 64)

    def test_urls(self):
        base = rp.VRE_BASE_URL
        self.assertEqual(
            rp.vre_url("b19001", 2014, "state"), f"{base}/2014/data/5-year/040/B19001.csv.gz"
        )
        self.assertEqual(
            rp.vre_url("B17001", 2023, "tract", "6"), f"{base}/2023/data/5-year/140/B17001_06.csv.zip"
        )
        self.assertEqual(rp.vre_url("B01001", 2024, "150", "72").rsplit("/", 1)[-1], "B01001_72.csv.zip")
        with self.assertRaises(ValueError):
            rp.vre_url("B17001", 2023, "tract")
        with self.assertRaises(ValueError):
            rp.vre_url("B17001", 2013, "state")
        with self.assertRaises(ValueError):
            rp.sumlev_for("planet")

    def test_availability(self):
        # Spot-checked against www2.census.gov (HTTP 404 vs 200) on 2026-11-02.
        self.assertFalse(rp.vre_available("B17001", 2024, "block_group"))
        self.assertTrue(rp.vre_available("B17001", 2024, "tract"))
        self.assertFalse(rp.vre_available("B05001PR", 2024, "us"))
        self.assertTrue(rp.vre_available("B05001PR", 2024, "state"))
        self.assertFalse(rp.vre_available("B16001", 2017, "county"))
        self.assertTrue(rp.vre_available("B16001", 2014, "county"))
        self.assertFalse(rp.vre_available("B19013", 2023, "county"))
        self.assertFalse(rp.vre_available("B19001", 2013, "county"))
        self.assertFalse(rp.vre_available("B19001", 2023, "planet"))
        info = rp.vre_availability("B19001", 2023, "county")
        self.assertTrue(info["available"])
        self.assertEqual(info["url"], f"{rp.VRE_BASE_URL}/2023/data/5-year/050/B19001.csv.zip")
        tract = rp.vre_availability("B19001", 2023, "tract")
        self.assertTrue(tract["per_state_files"])
        self.assertIn("B19001_{SS}.csv.zip", tract["url_template"])
        self.assertIn("not in the", rp.vre_availability("B19013", 2023, "county")["reason"])


class TestGeoid(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(rp.normalize_vre_geoid("0500000US48201"), "48201")
        self.assertEqual(rp.normalize_vre_geoid("05000US48201"), "48201")
        self.assertEqual(rp.normalize_vre_geoid("1400000US36061000100"), "36061000100")
        self.assertEqual(rp.normalize_vre_geoid("8600000US01001"), "01001")
        self.assertEqual(rp.normalize_vre_geoid("0100000US"), "US")
        self.assertEqual(rp.normalize_vre_geoid("01000US"), "US")

    def test_matches_build_geoid(self):
        self.assertEqual(
            rp.normalize_vre_geoid("0500000US48201"), build_geoid({"state": "48", "county": "201"}, "county")
        )
        self.assertEqual(rp.normalize_vre_geoid("0100000US"), build_geoid({}, "us"))


class TestFixtureParse(unittest.TestCase):
    def test_sidecars_match_fixture_bytes(self):
        for path in ALL_FIXTURES:
            with open(path, "rb") as f:
                raw = f.read()
            with open(path + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["fixture_sha256"], path)
            self.assertTrue(meta["source_url"].startswith(rp.VRE_BASE_URL))
            self.assertEqual(len(meta["source_sha256"]), 64)
            self.assertLess(len(raw), 200 * 1024)

    def test_contract_columns(self):
        for path in ALL_FIXTURES:
            frame = _frame(path)
            self.assertEqual(list(frame.columns[: len(rp.REPLICATE_FRAME_COLUMNS)]), rp.REPLICATE_FRAME_COLUMNS)
            self.assertEqual(
                list(frame.columns[len(rp.REPLICATE_FRAME_COLUMNS):]), rp.REPLICATE_FRAME_EXTRA_COLUMNS
            )
            self.assertFalse(frame[["estimate", "moe"] + REPLICATE_COLUMNS].isna().any().any())

    def test_2023_state(self):
        frame = _frame(F2023)
        self.assertEqual(len(frame), 5 * 17)
        self.assertEqual(sorted(frame["GEOID"].unique()), ["01", "10", "11", "44", "72"])
        self.assertEqual(list(frame["variable"].unique()), [f"B19001_{i:03d}" for i in range(1, 18)])
        al = frame[(frame["GEOID"] == "01") & (frame["variable"] == "B19001_001")].iloc[0]
        self.assertEqual(al["NAME"], "Alabama")
        self.assertEqual(al["estimate"], 1969105)
        self.assertEqual(al["moe"], 6574)
        self.assertEqual(al["se_published"], 3996)
        self.assertEqual(frame.attrs["geoid_prefix"], "0400000US")

    def test_2014_county_lowercase_header_and_latin1(self):
        frame = _frame(F2014)
        self.assertEqual(sorted(frame["GEOID"].unique()), ["05117", "35013", "72011"])
        names = set(frame["NAME"])
        self.assertIn("Doña Ana County, New Mexico", names)
        self.assertIn("Añasco Municipio, Puerto Rico", names)
        self.assertEqual(frame.attrs["geoid_prefix"], "05000US")

    def test_controlled_flag(self):
        frame = _frame(F2018)
        ctrl = frame[frame["controlled"]]
        self.assertEqual(sorted(ctrl["GEOID"]), ["48017", "48087"])
        self.assertTrue((ctrl["variable"] == "B01001_001").all())
        self.assertTrue((ctrl["moe"] == 0).all())
        loving = frame[(frame["GEOID"] == "48301") & (frame["variable"] == "B01001_001")].iloc[0]
        self.assertFalse(loving["controlled"])

    def test_rejects_non_vre(self):
        with self.assertRaises(rp.VREDownloadError):
            rp.read_vre_csv(b"<html><body>not a csv</body></html>")


class TestPublishedMoe(unittest.TestCase):
    """Validation check F3: replicate MOEs reproduce published MOEs to rounding."""

    def test_fixtures_reproduce(self):
        for path in ALL_FIXTURES:
            report = rp.verify_published_moe(_frame(path))
            self.assertTrue(report["passed"], (path, report["mismatches"]))
            self.assertGreater(report["rows_checked"], 40)
            self.assertLessEqual(report["max_abs_diff"], 0.5)

    def test_skips_controlled_and_model_based(self):
        report = rp.verify_published_moe(_frame(F2018))
        self.assertEqual(report["controlled_rows"], 2)
        self.assertGreater(report["model_based_rows"], 0)
        self.assertEqual(
            report["rows"],
            report["rows_checked"] + report["controlled_rows"] + report["model_based_rows"]
            + report["rows_without_published_moe"],
        )
        report14 = rp.verify_published_moe(_frame(F2014))
        self.assertEqual(report14["model_based_rows"], 2)  # the two zero-count B19001_017 cells

    def test_detects_a_wrong_moe(self):
        frame = _frame(F2023)
        frame.loc[3, "moe"] += 5
        report = rp.verify_published_moe(frame)
        self.assertFalse(report["passed"])
        self.assertEqual(report["n_mismatch"], 1)
        self.assertEqual(report["mismatches"][0]["variable"], frame.loc[3, "variable"])


class TestReplicateStatistics(unittest.TestCase):
    def setUp(self):
        self.frame = _frame(F2023)

    def test_sum_of_parts_reproduces_published_total(self):
        # Replicates are additive within a table, so summing the 16 income
        # brackets reproduces the published total and its published MOE.
        out = rp.replicate_sum(self.frame, B19001_PARTS)
        totals = self.frame[self.frame["variable"] == "B19001_001"].set_index("GEOID")
        self.assertEqual(list(out.columns), ["GEOID", "NAME"] + rp.DERIVED_COLUMNS)
        for _, row in out.iterrows():
            self.assertEqual(row["estimate"], totals.loc[row["GEOID"], "estimate"])
            self.assertLessEqual(abs(row["moe"] - totals.loc[row["GEOID"], "moe"]), 0.5)
            self.assertEqual(row["method"], "replicate")
            self.assertAlmostEqual(row["se"], row["moe"] / Z90)
            self.assertAlmostEqual(row["cv_percent"], row["se"] / row["estimate"] * 100)

    def test_exact_vs_approximation_for_correlated_cells(self):
        # Brackets of one table are negatively correlated, so the naive
        # sqrt(sum MOE^2) differs from the exact replicate MOE.
        out = rp.replicate_sum(self.frame, B19001_PARTS, geoids=["01"])
        parts = self.frame[(self.frame["GEOID"] == "01") & self.frame["variable"].isin(B19001_PARTS)]
        naive = math.sqrt(float((parts["moe"] ** 2).sum()))
        self.assertGreater(abs(naive - out.loc[0, "moe"]) / out.loc[0, "moe"], 0.05)

    def test_sum_across_geographies(self):
        geos = ["10", "11", "44"]
        out = rp.replicate_sum(self.frame, ["B19001_017"], geoids=geos, across_geographies=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.loc[0, "GEOID"], "10,11,44")
        sub = self.frame[(self.frame["variable"] == "B19001_017") & self.frame["GEOID"].isin(geos)]
        est = sub["estimate"].sum()
        reps = sub[REPLICATE_COLUMNS].to_numpy().sum(axis=0)
        self.assertEqual(out.loc[0, "estimate"], est)
        self.assertAlmostEqual(out.loc[0, "moe"], _manual_moe(est, reps), places=9)

    def test_difference(self):
        out = rp.replicate_difference(self.frame, "B19001_001", B19001_PARTS[:8], geoids=["44"])
        d = self.frame[self.frame["GEOID"] == "44"].set_index("variable")
        est = d.loc["B19001_001", "estimate"] - d.loc[B19001_PARTS[:8], "estimate"].sum()
        reps = d.loc["B19001_001", REPLICATE_COLUMNS].to_numpy(float) - d.loc[
            B19001_PARTS[:8], REPLICATE_COLUMNS
        ].to_numpy(float).sum(axis=0)
        self.assertEqual(out.loc[0, "estimate"], est)
        self.assertAlmostEqual(out.loc[0, "moe"], _manual_moe(est, reps), places=9)
        # The difference equals the sum of the remaining brackets, replicate by replicate.
        rest = rp.replicate_sum(self.frame, B19001_PARTS[8:], geoids=["44"])
        self.assertAlmostEqual(out.loc[0, "moe"], rest.loc[0, "moe"], places=6)

    def test_proportion_and_ratio(self):
        high = ["B19001_016", "B19001_017"]
        prop = rp.replicate_proportion(self.frame, high, "B19001_001", as_percent=True)
        d = self.frame[self.frame["GEOID"] == "11"].set_index("variable")
        num = d.loc[high, ["estimate"] + REPLICATE_COLUMNS].to_numpy(float).sum(axis=0)
        den = d.loc["B19001_001", ["estimate"] + REPLICATE_COLUMNS].to_numpy(float)
        pct = 100 * num / den
        row = prop.set_index("GEOID").loc["11"]
        self.assertAlmostEqual(row["estimate"], pct[0])
        self.assertAlmostEqual(row["moe"], _manual_moe(pct[0], pct[1:]), places=9)
        ratio = rp.replicate_ratio(self.frame, high, "B19001_001", geoids=["11"])
        self.assertAlmostEqual(ratio.loc[0, "estimate"] * 100, row["estimate"])
        self.assertAlmostEqual(ratio.loc[0, "moe"] * 100, row["moe"])

    def test_proportion_flags_non_subset(self):
        out = rp.replicate_proportion(self.frame, "B19001_001", "B19001_002", geoids=["01"])
        self.assertTrue(any("not_a_subset" in n for n in out.loc[0, "notes"]))

    def test_missing_rows_raise(self):
        with self.assertRaises(KeyError):
            rp.replicate_sum(self.frame, ["B19001_001"], geoids=["99"])
        with self.assertRaises(ValueError):
            rp.replicate_sum(self.frame, [])


class TestReplicateApply(unittest.TestCase):
    def setUp(self):
        self.frame = _frame(F2023)

    def test_per_geography_matches_replicate_sum(self):
        out = rp.replicate_apply(self.frame, lambda m: m.sum(axis=1), variables=B19001_PARTS)
        ref = rp.replicate_sum(self.frame, B19001_PARTS)
        pd.testing.assert_series_equal(out["moe"], ref["moe"])
        self.assertEqual(list(out["GEOID"]), list(ref["GEOID"]))

    def test_dict_mode_and_scalar(self):
        func = lambda d: (d["B19001_016"] + d["B19001_017"]).sum() / d["B19001_001"].sum()  # noqa: E731
        out = rp.replicate_apply(
            self.frame, func, variables=["B19001_001", "B19001_016", "B19001_017"], as_dict=True
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out.loc[0, "GEOID"], "01,10,11,44,72")
        ref = rp.replicate_ratio(
            self.frame, ["B19001_016", "B19001_017"], "B19001_001", across_geographies=True
        )
        self.assertAlmostEqual(out.loc[0, "estimate"], ref.loc[0, "estimate"])
        self.assertAlmostEqual(out.loc[0, "moe"], ref.loc[0, "moe"])

    def test_other_shapes(self):
        out = rp.replicate_apply(self.frame, lambda m: m.sum(axis=0), variables=B19001_PARTS[:3])
        self.assertEqual(list(out["index"]), [0, 1, 2])
        with self.assertRaises(ValueError):
            rp.replicate_apply(self.frame, lambda m: m, variables=B19001_PARTS[:3])

    def test_documentation_example_1_arithmetic(self):
        # VRE documentation Example 1: sum of squared differences 5,446,546
        # -> variance 272,327.3, SE 521.8499, MOE 858.4431.
        reps = [94433.0] * 80
        reps[0] = 94433.0 + math.sqrt(5446546.0)
        frame = _synthetic([("X", "T_001", 94433.0, 858.0, reps)])
        out = rp.replicate_apply(frame, lambda m: m[:, 0])
        self.assertAlmostEqual(out.loc[0, "se"], 521.8499, places=4)
        self.assertAlmostEqual(out.loc[0, "moe"], 858.4431, places=4)


class TestSpecialCases(unittest.TestCase):
    def test_controlled_sum_has_zero_moe(self):
        frame = _frame(F2018)
        out = rp.replicate_sum(frame, "B01001_001", geoids=["48017", "48087"], across_geographies=True)
        self.assertEqual(out.loc[0, "moe"], 0.0)
        self.assertTrue(any(n.startswith("controlled") for n in out.loc[0, "notes"]))
        mixed = rp.replicate_sum(frame, "B01001_001", geoids=["48017", "48301"], across_geographies=True)
        self.assertGreater(mixed.loc[0, "moe"], 0)

    def test_zero_estimate_sum_uses_largest_published_moe(self):
        # Documentation Example 5 (two zero-count cells, published MOEs 16 and 24).
        frame = _frame(F2014)
        out = rp.replicate_sum(frame, "B19001_017", geoids=["05117", "72011"], across_geographies=True)
        self.assertEqual(out.loc[0, "estimate"], 0)
        self.assertEqual(out.loc[0, "moe"], 24.0)
        self.assertTrue(any(n.startswith("zero_estimate") for n in out.loc[0, "notes"]))

    def test_zero_variance_nonzero_estimate_is_undefined(self):
        # Documentation Example 6: uncontrolled, calculated MOE 0 -> model-based MOE needed.
        frame = _synthetic(
            [("A", "T_001", 725.0, 27.0, [725.0] * 80), ("B", "T_001", 231.0, 21.0, [231.0] * 80)]
        )
        out = rp.replicate_sum(frame, "T_001", across_geographies=True)
        self.assertEqual(out.loc[0, "estimate"], 956)
        self.assertTrue(math.isnan(out.loc[0, "moe"]))
        self.assertTrue(any(n.startswith("zero_replicate_variance") for n in out.loc[0, "notes"]))

    def test_zero_replicate_denominator_set_to_zero(self):
        # Documentation Example 7: an undefined replicate ratio is replaced by 0.
        num = [30.0] * 80
        den = [50.0] * 80
        den[1] = 0.0
        frame = _synthetic([("A", "T_002", 40.0, 5.0, num), ("A", "T_001", 50.0, 5.0, den)])
        out = rp.replicate_ratio(frame, "T_002", "T_001")
        est = 0.8
        reps = np.array(num) / np.where(np.array(den) == 0, np.nan, den)
        reps[1] = 0.0
        self.assertAlmostEqual(out.loc[0, "estimate"], est)
        self.assertAlmostEqual(out.loc[0, "moe"], _manual_moe(est, reps), places=12)
        self.assertTrue(any("zero_replicate_denominator" in n for n in out.loc[0, "notes"]))

    def test_zero_denominator_estimate(self):
        frame = _synthetic([("A", "T_002", 0.0, 5.0, [0.0] * 80), ("A", "T_001", 0.0, 5.0, [1.0] * 80)])
        out = rp.replicate_ratio(frame, "T_002", "T_001")
        self.assertTrue(math.isnan(out.loc[0, "estimate"]))
        self.assertTrue(math.isnan(out.loc[0, "moe"]))
        self.assertTrue(any("zero_denominator" in n for n in out.loc[0, "notes"]))


class TestUncorrelatedEqualsApproximation(unittest.TestCase):
    """With uncorrelated components the replicate result equals the handbook formulas."""

    def setUp(self):
        rng = np.random.default_rng(7)
        self.x, self.y = 400.0, 2000.0
        dx = np.zeros(80)
        dy = np.zeros(80)
        dx[:40] = rng.normal(0, 3.0, 40)  # disjoint supports: zero replicate covariance
        dy[40:] = rng.normal(0, 8.0, 40)
        self.rx, self.ry = self.x + dx, self.y + dy
        self.frame = _synthetic(
            [("A", "T_002", self.x, 0.0, self.rx), ("A", "T_001", self.y, 0.0, self.ry)]
        )
        self.mx, self.my = _manual_moe(self.x, self.rx), _manual_moe(self.y, self.ry)

    def test_sum_and_difference_exact(self):
        s = rp.replicate_sum(self.frame, ["T_001", "T_002"])
        self.assertAlmostEqual(s.loc[0, "moe"], math.hypot(self.mx, self.my), places=9)
        d = rp.replicate_difference(self.frame, "T_001", "T_002")
        self.assertAlmostEqual(d.loc[0, "moe"], math.hypot(self.mx, self.my), places=9)

    def test_ratio_matches_formula(self):
        r = self.x / self.y
        approx = (1 / self.y) * math.sqrt(self.mx**2 + r**2 * self.my**2)
        out = rp.replicate_ratio(self.frame, "T_002", "T_001")
        self.assertAlmostEqual(out.loc[0, "estimate"], r)
        self.assertLess(abs(out.loc[0, "moe"] - approx) / approx, 1e-3)


class FakeResponse:
    def __init__(self, content, status=200, ctype="application/zip"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": ctype}


class FakeSession:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if url not in self.responses:
            return FakeResponse(b"", status=404, ctype="text/html")
        return self.responses[url]


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(F2023, "rb") as f:
            self.raw = f.read()
        self.url = rp.vre_url("B19001", 2023, "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_download_cache_and_reuse(self):
        session = FakeSession({self.url: FakeResponse(self.raw)})
        frame = rp.fetch_vre("B19001", 2023, "state", session=session, cache_dir=self.tmp)
        self.assertEqual(len(frame), 85)
        self.assertEqual(frame.attrs["source_url"], self.url)
        self.assertEqual(frame.attrs["sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertTrue(frame.attrs["moe_check"]["passed"])
        cached = os.path.join(self.tmp, "040", "B19001.csv.zip")
        with open(cached + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["source_url"], self.url)
        self.assertEqual(meta["sha256"], hashlib.sha256(self.raw).hexdigest())
        # Second call is served from cache.
        frame2 = rp.fetch_vre("B19001", 2023, "state", session=session, cache_dir=self.tmp)
        self.assertEqual(len(session.calls), 1)
        pd.testing.assert_frame_equal(frame, frame2)

    def test_tampered_cache_is_redownloaded(self):
        session = FakeSession({self.url: FakeResponse(self.raw)})
        rp.fetch_vre("B19001", 2023, "state", session=session, cache_dir=self.tmp)
        cached = os.path.join(self.tmp, "040", "B19001.csv.zip")
        with open(cached, "ab") as f:
            f.write(b"tamper")
        rp.fetch_vre("B19001", 2023, "state", session=session, cache_dir=self.tmp)
        self.assertEqual(len(session.calls), 2)

    def test_state_filter_on_national_file(self):
        session = FakeSession({self.url: FakeResponse(self.raw)})
        frame = rp.fetch_vre("B19001", 2023, "state", state_code="1", session=session, cache_dir=self.tmp)
        self.assertEqual(set(frame["GEOID"]), {"01"})
        with self.assertRaises(ValueError):
            rp.fetch_vre("B19001", 2023, "zcta", state_code="01", session=session, cache_dir=self.tmp)

    def test_per_state_file_url(self):
        url = rp.vre_url("B17001", 2023, "tract", "10")
        session = FakeSession({url: FakeResponse(self.raw)})
        rp.fetch_vre("B17001", 2023, "tract", state_code="10", session=session, cache_dir=self.tmp, check_moe=False)
        self.assertEqual(session.calls, [url])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "140", "B17001_10.csv.zip.json")))

    def test_gzip_release(self):
        url = rp.vre_url("B19001", 2014, "county")
        with open(F2014, "rb") as f:
            raw = f.read()
        session = FakeSession({url: FakeResponse(raw, ctype="application/x-tar")})
        frame = rp.fetch_vre("B19001", 2014, "county", state_code="35", session=session, cache_dir=self.tmp)
        self.assertEqual(set(frame["GEOID"]), {"35013"})

    def test_errors(self):
        rejected = FakeResponse(b"<html><head><title>Request Rejected</title></head></html>", ctype="text/html")
        with self.assertRaises(rp.VREDownloadError) as ctx:
            rp.fetch_vre("B19001", 2023, "state", session=FakeSession({self.url: rejected}), cache_dir=self.tmp)
        self.assertIn("rejected", str(ctx.exception))
        with self.assertRaises(rp.VREDownloadError):
            rp.fetch_vre("B19001", 2023, "state", session=FakeSession({}), cache_dir=self.tmp)
        with self.assertRaises(rp.VREDownloadError):
            html = FakeResponse(b"<html>maintenance</html>", ctype="text/html")
            rp.fetch_vre("B19001", 2023, "state", session=FakeSession({self.url: html}), cache_dir=self.tmp)
        with self.assertRaises(ValueError):
            rp.fetch_vre("B19013", 2023, "state", session=FakeSession({}), cache_dir=self.tmp)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "040", "B19001.csv.zip")))

    def test_moe_warning_on_load(self):
        # Serve an uncompressed copy whose first data row has a wrong MOE.
        with zipfile.ZipFile(io.BytesIO(self.raw)) as zf:
            text = zf.read(zf.infolist()[0]).decode("latin-1").split("\n")
        parts = text[3].split(",")
        parts[6] = str(int(parts[6]) + 10)  # TBLID,GEOID,NAME,ORDER,TITLE,ESTIMATE,MOE
        text[3] = ",".join(parts)
        corrupted = "\n".join(text).encode("latin-1")
        session = FakeSession({self.url: FakeResponse(corrupted, ctype="text/csv")})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = rp.fetch_vre("B19001", 2023, "state", session=session, cache_dir=self.tmp)
        self.assertFalse(out.attrs["moe_check"]["passed"])
        self.assertTrue(any("published MOE" in str(w.message) for w in caught))


class TestTableList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refresh_reads_published_csv(self):
        frozen = rp.vre_tables(2024)
        body = ("TBLID,TITLE\r\n" + "".join(f"{t},Title\r\n" for t in frozen)).encode()
        url = rp.VRE_LAYOUTS[2024]["table_list_url"]
        session = FakeSession({url: FakeResponse(body, ctype="text/csv")})
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(rp.vre_tables(2024, refresh=True, session=session, cache_dir=self.tmp), frozen)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "VRE_TABLE_LIST_2024.csv.json")))
        # Cached list is used afterwards without network access.
        self.assertEqual(rp.vre_tables(2024, session=FakeSession({}), cache_dir=self.tmp), frozen)

    def test_refresh_warns_on_change(self):
        body = b'TBLID,TITLE\n"B99999","New"\n"B01001","SEX BY AGE"\n'
        url = rp.VRE_LAYOUTS[2019]["table_list_url"]
        session = FakeSession({url: FakeResponse(body, ctype="text/csv")})
        with self.assertWarns(UserWarning):
            out = rp.vre_tables(2019, refresh=True, session=session, cache_dir=self.tmp)
        self.assertEqual(out, ["B99999", "B01001"])

    def test_pre_2018_has_no_csv(self):
        session = FakeSession({})
        self.assertEqual(rp.vre_tables(2016, refresh=True, session=session, cache_dir=self.tmp), rp.vre_tables(2016))
        self.assertEqual(session.calls, [])



REJECTED_PAGE = (b"<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. "
                 b"Your support ID is: 13979921158196382640<br><br></body></html>")


def _vre_zip(table, title, lines, seed=0):
    """Synthetic 2019 tract VRE zip for Michigan: ``lines`` = [(ORDER, TITLE)], 3 tracts, MOEs consistent."""
    rng = np.random.default_rng(seed)
    head = ["TBLID", "GEOID", "NAME", "ORDER", "TITLE", "ESTIMATE", "MOE", "CME", "SE"] + [
        f"Var_Rep{i}" for i in range(1, 81)]
    out = [",".join(head), f"{table},,,,{title}" + "," * (len(head) - 5),
           f"{table},,,,Universe:  Total population" + "," * (len(head) - 5)]
    for k in range(3):
        for order, ltitle in lines:
            est = 1000 + 300 * k + 10 * order
            reps = est + rng.integers(-60, 61, 80)
            se = math.sqrt(4.0 / 80.0 * float(np.sum((reps - est) ** 2)))
            moe = round(Z90 * se)
            out.append(",".join([table, f"14000US2616300{k}100", f"\"Census Tract {k}, Wayne County, Michigan\"",
                                 str(order), ltitle, str(est), str(moe), f"+/-{moe}", str(round(se))]
                                + [str(int(r)) for r in reps]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{table}_26.csv", "\n".join(out) + "\n")
    return buf.getvalue()


class TestTotalFallback(unittest.TestCase):
    """B01003 line 1 <- B01001 line 1 when census.gov rejects (or 404s) the B01003 file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.url3 = rp.vre_url("B01003", 2019, "tract", "26")
        self.url1 = rp.vre_url("B01001", 2019, "tract", "26")
        # Identical pair: same seed, same line-1 values; B01001 has more lines after line 1.
        self.b01003 = _vre_zip("B01003", "TOTAL POPULATION", [(1, "Total")], seed=3)
        self.b01001 = _vre_zip("B01001", "SEX BY AGE", [(1, "Total")], seed=3)
        self.b01001 = self._add_lines(self.b01001)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _add_lines(raw):
        """B01001 copy whose line 1 is titled 'Total:' and which has a line 2 per tract."""
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            rows = zf.read("B01001_26.csv").decode().rstrip("\n").split("\n")
        out = rows[:3]
        for r in rows[3:]:
            out.append(r.replace(",1,Total,", ",1,Total:,"))
            parts = r.replace(",1,Total,", ",2,Male:,").split(",")
            out.append(",".join(parts))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("B01001_26.csv", "\n".join(out) + "\n")
        return buf.getvalue()

    def _fetch(self, session, **kw):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            frame = rp.fetch_vre("B01003", 2019, "tract", "26", session=session, cache_dir=self.tmp, **kw)
        return frame, [str(w.message) for w in caught]

    def _check_substituted(self, frame, msgs, reason):
        sub = frame.attrs["substitution"]
        self.assertEqual(sub["requested_table"], "B01003")
        self.assertEqual(sub["requested_url"], self.url3)
        self.assertEqual(sub["source_table"], "B01001")
        self.assertEqual(sub["lines"], {"B01003_001": "B01001_001"})
        self.assertEqual(sub["source_url"], self.url1)
        self.assertEqual(sub["source_sha256"], hashlib.sha256(self.b01001).hexdigest())
        self.assertIn(reason, sub["reason"])
        self.assertIn("0 differing cells", sub["verification"])
        self.assertEqual(frame.attrs["source_url"], self.url1)  # the file actually read
        self.assertEqual(frame.attrs["sha256"], sub["source_sha256"])
        self.assertEqual(frame.attrs["table"], "B01003")
        self.assertTrue(frame.attrs["notes"][0].startswith("substituted_source:"))
        self.assertTrue(any("substitute" in m and self.url1 in m for m in msgs))  # never silent
        self.assertEqual(set(frame["variable"]), {"B01003_001"})
        self.assertEqual(set(frame["title"]), {"Total"})
        self.assertTrue(frame.attrs["moe_check"]["passed"])
        # The B01003 path holds nothing: the substitute is not cached under the requested name.
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "140", "B01003_26.csv.zip")))

    def test_rejection_uses_b01001_with_provenance(self):
        page = FakeResponse(REJECTED_PAGE, ctype="text/html")
        frame, msgs = self._fetch(FakeSession({self.url3: page, self.url1: FakeResponse(self.b01001)}))
        self._check_substituted(frame, msgs, "13979921158196382640")
        self.assertEqual(frame.attrs["substitution"]["support_id"], "13979921158196382640")
        self.assertIsNone(frame.attrs["substitution"]["http_status"])

    def test_404_uses_b01001(self):
        frame, msgs = self._fetch(FakeSession({self.url1: FakeResponse(self.b01001)}))
        self._check_substituted(frame, msgs, "404")
        self.assertEqual(frame.attrs["substitution"]["http_status"], 404)
        # Cached B01001 is reused on the next call (B01003 is requested again, then B01001 from the cache).
        s2 = FakeSession({})
        again, _ = self._fetch(s2)
        self.assertEqual(s2.calls, [self.url3])
        pd.testing.assert_frame_equal(again, frame)

    def test_relabelled_frame_equals_b01003_frame(self):
        direct = rp.fetch_vre("B01003", 2019, "tract", "26", cache_dir=self.tmp,
                              session=FakeSession({self.url3: FakeResponse(self.b01003)}))
        shutil.rmtree(os.path.join(self.tmp, "140"))
        frame, _ = self._fetch(FakeSession({self.url1: FakeResponse(self.b01001)}))
        pd.testing.assert_frame_equal(frame, direct, check_like=False)

    def test_substitute_unavailable_raises_original_error(self):
        page = FakeResponse(REJECTED_PAGE, ctype="text/html")
        with self.assertRaises(rp.VREDownloadError) as ctx:
            self._fetch(FakeSession({self.url3: page}))  # B01001: 404
        self.assertTrue(ctx.exception.rejected)
        self.assertEqual(ctx.exception.url, self.url3)
        self.assertIn("13979921158196382640", str(ctx.exception))
        with self.assertRaises(rp.VREDownloadError) as ctx:
            self._fetch(FakeSession({}))
        self.assertIn(self.url3, str(ctx.exception))

    def test_other_lines_tables_and_opt_out_do_not_fall_back(self):
        page = FakeResponse(REJECTED_PAGE, ctype="text/html")
        session = FakeSession({self.url3: page, self.url1: FakeResponse(self.b01001)})
        with self.assertRaises(rp.VREDownloadError) as ctx:
            self._fetch(session, lines=[1, 2])
        self.assertIn("No verified substitute", str(ctx.exception))
        self.assertIn("[2]", str(ctx.exception))
        self.assertNotIn(self.url1, session.calls)
        with self.assertRaises(rp.VREDownloadError):
            self._fetch(session, allow_substitute=False)
        self.assertNotIn(self.url1, session.calls)
        url = rp.vre_url("B01001", 2019, "tract", "26")
        with self.assertRaises(rp.VREDownloadError):  # B01001 itself has no substitute
            rp.fetch_vre("B01001", 2019, "tract", "26", session=FakeSession({url: page}), cache_dir=self.tmp)
        # Line 1 requested explicitly: substitute used.
        frame, _ = self._fetch(session, lines=[1])
        self.assertIn("substitution", frame.attrs)

    def test_other_failures_do_not_fall_back(self):
        session = FakeSession({self.url3: FakeResponse(b"", status=500, ctype="text/html"),
                               self.url1: FakeResponse(self.b01001)})
        with self.assertRaises(rp.VREDownloadError) as ctx:
            self._fetch(session)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertNotIn(self.url1, session.calls)

    def test_lines_filter_without_substitution(self):
        session = FakeSession({self.url1: FakeResponse(self.b01001)})
        frame = rp.fetch_vre("B01001", 2019, "tract", "26", session=session, cache_dir=self.tmp, lines=[2])
        self.assertEqual(set(frame["variable"]), {"B01001_002"})
        self.assertNotIn("substitution", frame.attrs)


if __name__ == "__main__":
    unittest.main()
