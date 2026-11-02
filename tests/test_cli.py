"""Tests for the python -m census command-line interface."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from census.__main__ import EXIT_INPUT, EXIT_OK, main


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), patch.dict(os.environ, {"CENSUS_CACHE_DISABLED": "1"}):
        code = main(argv)
    return code, buf.getvalue()


class TestCLI(unittest.TestCase):
    def test_describe_json(self):
        code, out = run(["describe", "--code", "DP02_0002PE", "--year", "2019", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Married-couple", json.loads(out)["label"])

    def test_concordance_reports_relabel(self):
        code, out = run(
            ["concordance", "--table", "B27001", "--years", "2016-2017", "--variables", "B27001_006", "--json"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["issues"][0]["kind"], "relabeled")

    def test_invalid_geo_exit_code(self):
        code, out = run(["plan", "--table", "B19013", "--geo", "bogus", "--years", "2020", "--json"])
        self.assertEqual(code, EXIT_INPUT)
        self.assertEqual(json.loads(out)["error_code"], "INVALID_GEO_LEVEL")

    def test_vintages(self):
        code, out = run(["vintages", "--dataset", "subject", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(2017, json.loads(out)["years"])


if __name__ == "__main__":
    unittest.main()
