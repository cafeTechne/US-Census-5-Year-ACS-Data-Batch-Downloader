"""Unit tests for census.geo module."""

import os
import unittest
from census.geo import (
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


if __name__ == "__main__":
    unittest.main()
