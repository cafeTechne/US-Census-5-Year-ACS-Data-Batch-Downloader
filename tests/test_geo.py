"""Unit tests for census.geo module."""

import os
import unittest

import pandas as pd

from census.geo import (
    add_geoid_column,
    build_geoid,
    counties_for_state,
    detect_geo_level,
    resolve_zip,
    zctas_for_state,
    STATE_FILES,
    STATE_MAPPING,
    FIPS_MAPPING,
    extract_state_abbr_from_filename,
    get_local_places_file,
    load_places_from_local,
)


class TestCensusGeo(unittest.TestCase):
    def test_extract_state_abbr(self):
        self.assertEqual(
            extract_state_abbr_from_filename("st36_ny_place2020.txt"), "36"
        )
        self.assertEqual(
            extract_state_abbr_from_filename("st48_tx_place2020.txt"), "48"
        )

    def test_state_mapping_integrity(self):
        self.assertEqual(STATE_MAPPING.get("36"), "New York")
        self.assertEqual(STATE_MAPPING.get("48"), "Texas")
        self.assertEqual(FIPS_MAPPING.get("New York"), "36")
        self.assertEqual(FIPS_MAPPING.get("Texas"), "48")

    def test_get_local_places_file(self):
        ny_file = get_local_places_file("36")
        self.assertIsNotNone(ny_file)
        self.assertTrue(os.path.exists(ny_file))
        self.assertTrue(ny_file.endswith("st36_ny_place2020.txt"))

        invalid_file = get_local_places_file("99")
        self.assertIsNone(invalid_file)

    def test_load_places_from_local(self):
        ny_file = get_local_places_file("36")
        places = load_places_from_local(ny_file)
        self.assertGreater(len(places), 100)
        # Check first place tuple structure: (state_code, place_code, place_name, place_type, counties)
        first = places[0]
        self.assertEqual(len(first), 5)
        self.assertEqual(first[0], "36")
        self.assertIsInstance(first[2], str)



class TestGEOIDs(unittest.TestCase):
    def test_county_geoids(self):
        self.assertEqual(build_geoid({"state": "36", "county": "061"}, "county"), "36061")
        self.assertEqual(build_geoid({"state": "48", "county": "453"}, "county"), "48453")
        # Zero-padding of unpadded components
        self.assertEqual(build_geoid({"state": "6", "county": "1"}, "county"), "06001")

    def test_tract_geoid_is_11_digits(self):
        geoid = build_geoid(
            {"state": "36", "county": "061", "tract": "000100"}, "tract"
        )
        self.assertEqual(geoid, "36061000100")
        self.assertEqual(len(geoid), 11)
        travis = build_geoid({"state": "48", "county": "453", "tract": "1101"}, "tract")
        self.assertEqual(travis, "48453001101")

    def test_place_zcta_msa_us(self):
        self.assertEqual(build_geoid({"state": "48", "place": "05000"}, "place"), "4805000")
        self.assertEqual(
            build_geoid({"zip code tabulation area": "501"}, "zcta"), "00501"
        )
        self.assertEqual(
            build_geoid(
                {"metropolitan statistical area/micropolitan statistical area": "12420"},
                "msa",
            ),
            "12420",
        )
        self.assertEqual(build_geoid({}, "us"), "US")

    def test_missing_component(self):
        with self.assertRaises(KeyError):
            build_geoid({"state": "36"}, "tract")

    def test_add_geoid_column(self):
        df = pd.DataFrame(
            {
                "NAME": ["Tract 1", "Tract 2"],
                "state": ["36", "36"],
                "county": ["061", "061"],
                "tract": ["000100", "000201"],
            }
        )
        self.assertEqual(detect_geo_level(df.columns), "tract")
        out = add_geoid_column(df)
        self.assertEqual(out.columns[0], "GEOID")
        self.assertEqual(out["GEOID"].tolist(), ["36061000100", "36061000201"])

    def test_add_geoid_from_geo_id_fallback(self):
        df = pd.DataFrame({"NAME": ["X"], "GEO_ID": ["8600000US10001"]})
        out = add_geoid_column(df)
        self.assertEqual(out["GEOID"].iloc[0], "10001")

    def test_add_geoid_empty_frame(self):
        out = add_geoid_column(pd.DataFrame(columns=["NAME", "state"]))
        self.assertIn("GEOID", out.columns)


class TestZipCrosswalk(unittest.TestCase):
    def test_resolve_zip(self):
        res = resolve_zip("10001")
        self.assertEqual(res["county_geoid"], "36061")
        self.assertEqual(res["state_name"], "New York")
        self.assertEqual(resolve_zip("78701")["county_geoid"], "48453")
        self.assertIsNone(resolve_zip("00000"))

    def test_zctas_for_state_and_county(self):
        dc = zctas_for_state("11")
        self.assertGreater(len(dc), 0)
        self.assertIn("20001", dc)
        manhattan = zctas_for_state("36", "061")
        self.assertIn("10001", manhattan)
        self.assertTrue(set(manhattan) <= set(zctas_for_state("36")))

    def test_counties_for_state(self):
        self.assertIn("453", counties_for_state("48"))


if __name__ == "__main__":
    unittest.main()
