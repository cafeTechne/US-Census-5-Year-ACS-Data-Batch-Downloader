# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACS errata and user notes (Round 9, F)."""

import json
import os
import unittest

from census import errata as E

LISTING = """
<ul class="cmp-list"><li><a class="x" title="t" href="/programs-surveys/acs/technical-documentation/errata/130.html">
<div class="uscb-tag-label">Errata Note</div>
<div class="cmp-list__item-title uscb-heading-2">Tabulation Processing Error in Table S2201 for 2015 to 2020 ACS</div>
<div class="uscb-list-text__item-metadata cmp-list__item-date">September 15, 2022</div>
<div class="cmp-list__item-description uscb-body-small-01">Errata Note 130</div></a></li>
<li><a href="https://www.census.gov/programs-surveys/acs/technical-documentation/errata.html">Errata Notes</a></li>
</ul>"""

NOTE = """<html><head><title>Tabulation Processing Error in Table S2201 for 2015 to 2020 ACS</title>
<meta name="description" property="og:description" content="Errata Note 130"/>
<meta name="DC.title" property="og:title" content="Tabulation Processing Error in Table S2201 for 2015 to 2020 ACS"/>
<meta name="DC.date.created" content="2022-09-15T00:00:00.000-04:00"/></head><body>
<nav>Errata Notes User Notes</nav><h1>Tabulation Processing Error in Table S2201 for 2015 to 2020 ACS</h1>
<p>September 15, 2022</p><p>There was a tabulation processing error in Table S2201 1-year and 5-year tables
released in 2015 through 2020. Data users can use Table B22002 to access the correct estimate. This error
has been corrected in Table S2201 in the 2021 tables.</p><p>Page Last Revised - September 6, 2022</p></body></html>"""

URL = "https://www.census.gov/programs-surveys/acs/technical-documentation/errata/130.html"


class TestParsing(unittest.TestCase):
    def test_listing(self):
        items = E.parse_listing(LISTING, "errata")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], URL)
        self.assertEqual(items[0]["date"], "September 15, 2022")
        self.assertEqual(items[0]["label"], "Errata Note 130")

    def test_note_record_uses_title_scope(self):
        rec = E.parse_note(NOTE, URL, "errata", "2026-11-02")
        self.assertEqual(rec["id"], "errata-130")
        self.assertEqual(rec["tables"], ["S2201"])  # B22002 is only the suggested alternative
        self.assertEqual(rec["tables_mentioned"], ["B22002", "S2201"])
        self.assertEqual(rec["release_years"], list(range(2015, 2021)))  # not 2021 (the fix) nor 2022 (dates)
        self.assertEqual(rec["products"], ["1-year", "5-year"])
        self.assertEqual(rec["date_iso"], "2022-09-15")
        self.assertTrue(rec["excerpt"].startswith("September 15, 2022 There was"))
        self.assertNotIn("Page Last Revised", rec["excerpt"])

    def test_year_patterns(self):
        self.assertEqual(E.extract_release_years("Error in the 2015-2019 ACS 5-year estimates"), [2019])
        self.assertEqual(E.extract_release_years("2019 ACS 1-year, published October 3, 2023"), [2019])
        self.assertEqual(E.extract_release_years("data for 2018 to 2020"), [2018, 2019, 2020])

    def test_tables(self):
        self.assertEqual(E.extract_tables("B17001A, C17002, S1701, DP03 and CP05; not B1700 or 2019"),
                         ["B17001A", "C17002", "CP05", "DP03", "S1701"])


RECS = [
    {"id": "a", "kind": "errata", "tables": ["S2201"], "release_years": [2019, 2020], "products": ["5-year"],
     "date_iso": "2022-09-15", "url": URL},
    {"id": "b", "kind": "errata", "tables": ["B17001"], "release_years": [], "products": [],
     "date_iso": "2021-01-01", "url": URL},
    {"id": "c", "kind": "errata", "tables": ["B17001"], "release_years": [2019], "products": ["1-year"],
     "date_iso": "2020-01-01", "url": URL},
    {"id": "d", "kind": "user_note", "tables": [], "release_years": [2020], "products": ["5-year"],
     "date_iso": "2023-01-01", "url": URL},
]


class TestLookup(unittest.TestCase):
    def ids(self, *a, **k):
        return [r["id"] for r in E.errata_for(*a, records=RECS, **k)]

    def test_table_year_product(self):
        self.assertEqual(self.ids("S2201", [2020]), ["a"])
        self.assertEqual(self.ids("S2201", [2018]), [])
        self.assertEqual(self.ids("B17001_002E", [2019]), ["b"])  # 'c' is 1-year only
        self.assertEqual(self.ids("B17001A", [2019], product=None), ["b", "c"])  # iteration matches base

    def test_general_notes(self):
        self.assertEqual(self.ids("B01001", [2020]), [])
        self.assertEqual(self.ids("B01001", [2020], include_general=True), ["d"])

    def test_check_urls(self):
        self.assertTrue(E.check_urls(RECS)["skipped"])
        res = E.check_urls(RECS + [{"url": "https://www.census.gov/missing.html"}], live=True,
                           get=lambda u: 404 if "missing" in u else 200)
        self.assertFalse(res["passed"])
        self.assertEqual(res["failures"], [{"url": "https://www.census.gov/missing.html", "status": 404}])


class TestDataset(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(E.errata_path()):
            self.skipTest("data/acs_errata.json not generated")
        self.records = E.load_errata()

    def test_records_and_sidecar(self):
        self.assertTrue(self.records)
        for rec in self.records:
            self.assertTrue(rec["url"].startswith("https://www.census.gov/programs-surveys/acs/technical-documentation/"))
            self.assertIn(rec["kind"], ("errata", "user_note"))
            self.assertTrue(rec["title"] and rec["retrieved_at"])
        self.assertEqual(len({r["url"] for r in self.records}), len(self.records))
        with open(E.get_resource_path(E.SIDECAR_PATH), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["sha256"], E.sha256_file(E.errata_path()))
        self.assertEqual(sidecar["records"], len(self.records))

    @unittest.skipUnless(os.getenv("CENSUS_LIVE_TESTS") == "1", "live check (CENSUS_LIVE_TESTS=1)")
    def test_every_cited_note_resolves(self):
        res = E.check_urls(self.records, live=True)
        self.assertTrue(res["passed"], res["failures"][:5])


if __name__ == "__main__":
    unittest.main()
