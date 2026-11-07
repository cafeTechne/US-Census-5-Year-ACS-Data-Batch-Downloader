# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.rpp: data integrity, lookups and advisories, and reproduction of BEA real personal income.

Reference: BEA table SARPI line 1, real personal income by state (millions of
constant 2017 dollars), in https://apps.bea.gov/regional/zip/SARPP.zip
(last updated February 19, 2026), and SAINC1 line 1 nominal personal income
(https://apps.bea.gov/regional/zip/SAINC.zip). BEA footnote: "Real personal
income for states is personal income divided by the RPPs and the national
PCE price index." The state values are additionally scaled by one common
factor per year so that they sum to U.S. real personal income; with that
step 2008 reproduces to rounding (2008 is the year both files share a
vintage: SAINC1 was revised for 2009-2025 on 2026-09-30).
"""

import csv
import hashlib
import json
import os
import unittest

import pandas as pd

from census import rpp
from census.inflation import inflation_factor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "rpp", "bea_real_personal_income.csv")


class TestData(unittest.TestCase):
    def test_checksums_match_sidecar(self):
        info = rpp.price_parity_info()
        for name, digest in info["csv_sha256"].items():
            with open(os.path.join(rpp.price_parity_dir(), name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest, name)
        for table, src in info["sources"].items():
            with open(os.path.join(ROOT, src["file"]), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), src["sha256"], table)
            self.assertTrue(src["url"].startswith("https://apps.bea.gov/regional/zip/"))

    def test_coverage(self):
        df = rpp.load_price_parities()
        counts = df.groupby("geo_type")["geo_fips"].nunique().to_dict()
        self.assertEqual(counts["state"], 51)
        self.assertGreaterEqual(counts["msa"], 380)
        self.assertGreaterEqual(counts["state_nonmetro"], 45)
        self.assertEqual(df["year"].min(), 2008)
        self.assertTrue((df.loc[df.geo_type == "us", "rpp_all_items"] == 100.0).all())

    def test_csv_matches_raw_zip(self):
        import io
        import zipfile
        with zipfile.ZipFile(os.path.join(ROOT, "data", "price_parities", "source", "SARPP.zip")) as zf:
            name = next(n for n in zf.namelist() if n.startswith("SARPP_") and n.endswith(".csv"))
            rows = list(csv.reader(io.StringIO(zf.read(name).decode("latin-1"))))
        hdr = [h.strip() for h in rows[0]]
        ca = next(r for r in rows[1:] if r[0].strip().strip('"') == "06000" and r[4].strip() == "1")
        for y in (2008, 2022, 2024):
            self.assertAlmostEqual(rpp.place_parity("06", y)["rpp"], float(ca[hdr.index(str(y))]), places=3)


class TestLookups(unittest.TestCase):
    def test_geography_forms(self):
        a = rpp.place_parity("06", 2022)
        self.assertEqual(rpp.place_parity("California", 2022)["rpp"], a["rpp"])
        self.assertEqual(rpp.place_parity("06000", 2022)["rpp"], a["rpp"])
        self.assertEqual(rpp.place_parity("us", 2022)["rpp"], 100.0)
        la = rpp.place_parity("msa:31080", 2022)
        self.assertEqual(la["geo_type"], "msa")
        self.assertIn("Los Angeles", la["geo_name"])
        nm = rpp.place_parity("06", 2022, portion="nonmetro")
        self.assertEqual(nm["geo_type"], "state_nonmetro")
        self.assertEqual(rpp.place_parity("06999", 2022)["rpp"], nm["rpp"])

    def test_non_msa_falls_back_to_state_nonmetro_with_advisory(self):
        p = rpp.place_parity("99999", 2022, state="06")
        self.assertEqual(p["geo_fips"], "06999")
        self.assertTrue(p["advisories"] and "nonmetropolitan" in p["advisories"][0])
        with self.assertRaises(rpp.RPPUnavailable):
            rpp.place_parity("99999", 2022)

    def test_year_outside_coverage(self):
        with self.assertRaisesRegex(rpp.RPPUnavailable, "2008"):
            rpp.place_parity("06", 2005)

    def test_adjustments(self):
        r = rpp.place_parity("06", 2022)["rpp"]
        self.assertAlmostEqual(rpp.adjust_for_place(1000.0, "06", 2022), 1000.0 * 100 / r)
        v = rpp.adjust_for_place_and_time(1000.0, "06", 2022, 2024)
        self.assertAlmostEqual(v, 1000.0 * 100 / r * inflation_factor(2022, 2024))

    def test_describe_states_use_and_error(self):
        d = rpp.describe_place_adjustment("06", 2022, base_year=2024)
        self.assertFalse(d["applied_by_default"])
        text = " ".join(d["notes"])
        self.assertIn("PLACE ADJUSTMENT", text)
        self.assertIn("R-CPI-U-RS", text)
        self.assertIn("estimation error", text)

    def test_dataframe(self):
        df = pd.DataFrame({"geo": ["06", "msa:31080", "99999"], "inc": [100.0, 100.0, 100.0], "inc_moe": [10.0] * 3})
        out = rpp.adjust_dataframe_for_place(df, ["inc", "inc_moe"], 2022, "geo")
        self.assertTrue(pd.isna(out.loc[2, "inc"]))
        self.assertEqual(len(out.attrs["rpp_adjustment"]["unavailable"]), 1)
        self.assertAlmostEqual(out.loc[0, "inc_moe"] / out.loc[0, "inc"], 0.1)
        self.assertEqual(df.loc[0, "inc"], 100.0)  # input untouched


class TestBEARealPersonalIncome(unittest.TestCase):
    """Reproduce BEA SARPI 2008 real personal income from nominal PI, RPPs and the PCE price index."""

    @classmethod
    def setUpClass(cls):
        cls.ref = pd.read_csv(FIXTURE, comment="#", dtype={"geo_fips": str})
        cls.pce = rpp.load_pce_price_index()

    def _unscaled(self, row):
        geo = "us" if row.geo_fips == "00000" else row.geo_fips[:2]
        return rpp.adjust_for_place_and_time(row.personal_income_nominal, geo, int(row.year), 2017,
                                             cpi_table=self.pce)

    def test_us_real_personal_income(self):
        us = self.ref[self.ref.geo_fips == "00000"].iloc[0]
        self.assertAlmostEqual(self._unscaled(us) / us.real_personal_income_published, 1.0, delta=1e-5)

    def test_states_with_bea_additivity(self):
        ref = self.ref[self.ref.year == 2008]
        states = ref[ref.geo_fips != "00000"].copy()
        self.assertEqual(len(states), 51)
        states["unscaled"] = [self._unscaled(r) for r in states.itertuples()]
        us_real = self._unscaled(ref[ref.geo_fips == "00000"].iloc[0])
        k = us_real / states["unscaled"].sum()
        self.assertAlmostEqual(k, 1.0, delta=0.002)  # the per-year scale is small
        rel = states["unscaled"] * k / states["real_personal_income_published"] - 1
        self.assertLess(rel.abs().max(), 2e-5)
        for fips in ("06000", "28000"):  # California, Mississippi
            row = states[states.geo_fips == fips].iloc[0]
            self.assertAlmostEqual(row.unscaled * k, row.real_personal_income_published,
                                   delta=2e-5 * row.real_personal_income_published)  # RPPs published to 3 decimals
            # unscaled formula alone is within 0.1%
            self.assertAlmostEqual(row.unscaled / row.real_personal_income_published, 1.0, delta=1e-3)


if __name__ == "__main__":
    unittest.main()
