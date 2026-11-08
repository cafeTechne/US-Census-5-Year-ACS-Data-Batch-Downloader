# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP ``cache_import`` (PLAN_ROUND_14 D2): the CLI ``cache import`` rules
(tests/test_review_fixes_r13.py TestCacheImportSafety, tests/test_reference_workflow.py
TestCacheImport) through the MCP tool and the CLI ``tool`` runner."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import mcp_server as srv
from census import mcp_profiles, workflows
from census.cache import QueryCache

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "reference_workflow", "frozen_api_responses.json")


class TestMcpCacheImport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "cache.db")

    def call(self, *args, env=None, remove=(), **kwargs):
        with mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": self.db} if env is None else env):
            for key in remove:
                os.environ.pop(key, None)
            return srv.cache_import(*args, **kwargs)

    def real_source(self):
        with open(FIXTURE, encoding="utf-8") as f:
            doc = json.load(f)
        del doc["synthetic"]
        path = os.path.join(self.dir, "real.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return path

    def test_registered_in_full_profile_only(self):
        self.assertIn("cache_import", {t.name for t in srv.mcp._tool_manager.list_tools()})
        for profile in ("core", "review"):
            server = mcp_profiles.build_profile_server(srv.mcp, profile)
            self.assertNotIn("cache_import", {t.name for t in server._tool_manager.list_tools()})

    def test_real_then_synthetic(self):
        out = self.call(self.real_source())
        self.assertTrue(out["success"], out)
        self.assertEqual(out["imported"], 2)
        self.assertFalse(out["synthetic"])
        self.assertIsNone(QueryCache(self.db).synthetic_marker())
        out = self.call(FIXTURE)
        self.assertTrue(out["synthetic"])
        self.assertEqual(out["entries"], 2)  # same request keys: replaced, not duplicated
        self.assertEqual(out["path"], self.db)

    def test_import_is_atomic(self):
        src = os.path.join(self.dir, "src")
        os.makedirs(src)
        shutil.copyfile(FIXTURE, os.path.join(src, "a_good.json"))
        with open(os.path.join(src, "b_bad.json"), "w", encoding="utf-8") as f:
            json.dump({"request": {"year": 2019, "colour": "red"}, "requests": {"B19013": [["GEO_ID"]]}}, f)
        out = self.call(src)
        self.assertFalse(out["success"])
        self.assertIn("b_bad.json", out["error"])
        cache = QueryCache(self.db)
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertIsNone(cache.synthetic_marker())

    def test_synthetic_into_default_cache_needs_flag(self):
        default = QueryCache(os.path.join(self.dir, "default.db"))
        with mock.patch.object(workflows, "get_default_cache", return_value=default):
            out = self.call(FIXTURE, env={}, remove=("CENSUS_CACHE_PATH",))
            self.assertFalse(out["success"])
            self.assertIn("CENSUS_CACHE_PATH", out["error"])
            self.assertIn("allow_default_cache=true", out["error"])
            self.assertEqual(default.stats()["entries"], 0)
            self.assertIsNone(default.synthetic_marker())
            out = self.call(self.real_source(), env={}, remove=("CENSUS_CACHE_PATH",))  # real: no flag needed
            self.assertTrue(out["success"], out)
            self.assertFalse(out["synthetic"])
            out = self.call(FIXTURE, allow_default_cache=True, env={}, remove=("CENSUS_CACHE_PATH",))
            self.assertTrue(out["success"], out)
            self.assertTrue(default.synthetic_marker())

    def test_errors(self):
        self.assertFalse(self.call("")["success"])
        self.assertFalse(self.call(os.path.join(self.dir, "missing.json"))["success"])
        out = self.call(self.dir)  # a directory without frozen files
        self.assertFalse(out["success"])
        self.assertIn("No frozen response files", out["error"])
        out = self.call(FIXTURE, env={"CENSUS_CACHE_DISABLED": "1"})
        self.assertFalse(out["success"])
        self.assertIn("disabled", out["error"])

    def test_cli_tool_runner(self):
        from census.__main__ import main

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CENSUS_CACHE_PATH": self.db}), contextlib.redirect_stdout(buf):
            code = main(["tool", "cache_import", "--arg", f"source={FIXTURE}", "--json"])
        out = json.loads(buf.getvalue())
        self.assertEqual(code, 0, out)
        self.assertTrue(out["success"] and out["synthetic"])
        self.assertEqual(out["imported"], 2)


if __name__ == "__main__":
    unittest.main()
