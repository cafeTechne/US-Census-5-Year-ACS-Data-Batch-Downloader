"""Unit tests for census.tables module."""

import os
import unittest
from census.geo import get_resource_path
from census.tables import (
    VARIABLE_DEFINITIONS,
    get_variable_descriptions,
    load_tables_from_local,
    load_variable_definitions,
)


class TestCensusTables(unittest.TestCase):
    def test_load_tables_from_local(self):
        groups_path = get_resource_path("groups.json")
        tables = load_tables_from_local(groups_path)
        self.assertIsInstance(tables, dict)
        self.assertIn("B01001", tables)
        self.assertIsInstance(tables["B01001"], str)

    def test_load_variable_definitions(self):
        vdefs = load_variable_definitions(force_reload=True)
        self.assertIsInstance(vdefs, dict)
        self.assertGreater(len(vdefs), 1000)

    def test_get_variable_descriptions(self):
        mock_defs = {
            "B01001_001E": "Total",
            "B01001_002E": "Male",
            "B02001_001E": "Race Total",
        }
        filtered = get_variable_descriptions("B01001", definitions=mock_defs)
        self.assertEqual(len(filtered), 2)
        self.assertIn("B01001_001E", filtered)
        self.assertIn("B01001_002E", filtered)
        self.assertNotIn("B02001_001E", filtered)

    def test_invalid_table_code_type(self):
        with self.assertRaises(ValueError):
            get_variable_descriptions(12345)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
