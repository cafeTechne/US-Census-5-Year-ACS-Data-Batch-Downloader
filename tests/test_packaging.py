# SPDX-License-Identifier: AGPL-3.0-or-later
"""pyproject.toml, version, entry points and release files (Round 9, H)."""

import importlib
import os
import re
import unittest

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

import census
from census import pipeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _requirement_lines():
    out = []
    for line in _read("requirements.txt").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.replace(" ", ""))
    return out


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class TestPyproject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            cls.doc = tomllib.load(f)

    def test_metadata(self):
        project = self.doc["project"]
        self.assertEqual(project["name"], "census-acs-toolkit")
        self.assertIn("version", project["dynamic"])
        self.assertEqual(self.doc["tool"]["setuptools"]["dynamic"]["version"]["attr"], "census.__version__")
        self.assertEqual(set(project["optional-dependencies"]["dev"]), {"pytest", "ruff"})

    def test_dependencies_match_requirements(self):
        deps = [d.replace(" ", "") for d in self.doc["project"]["dependencies"]]
        self.assertEqual(sorted(deps), sorted(_requirement_lines()))

    def test_entry_points_importable(self):
        scripts = self.doc["project"]["scripts"]
        self.assertEqual(scripts["census-acs"], "census.__main__:main")
        self.assertEqual(scripts["census-acs-mcp"], "mcp_server:main")
        for target in scripts.values():
            module, func = target.split(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)))
        self.assertIn("mcp_server", self.doc["tool"]["setuptools"]["py-modules"])

    def test_no_generic_top_level_packages(self):
        # Only the census package is installed; repository data is read in place
        # (editable install), so names like `data` or `docs` never reach site-packages.
        packages = self.doc["tool"]["setuptools"]["packages"]
        self.assertEqual(packages, ["census"])
        for folder in ("metadata", "data", "crosswalks", "docs/recipes", "state_places"):
            self.assertTrue(os.path.isdir(os.path.join(ROOT, folder)), folder)


class TestVersion(unittest.TestCase):
    def test_versions_agree(self):
        self.assertEqual(census.__version__, "0.9.0")
        self.assertEqual(pipeline.PACKAGE_VERSION, census.__version__)
        self.assertRegex(_read("CITATION.cff"), rf'(?m)^version: "{re.escape(census.__version__)}"$')
        self.assertIn(f"## {census.__version__} ", _read("CHANGELOG.md"))

    def test_release_workflow(self):
        text = _read(os.path.join(".github", "workflows", "release.yml"))
        self.assertIn('tags: ["v*"]', text)
        self.assertIn("python -m build", text)
        self.assertIn("gh release create", text)


if __name__ == "__main__":
    unittest.main()
