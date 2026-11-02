# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool profiles and the J2 design rules for the core profile (PLAN_ROUND_8 J1-J3)."""

import os
import unittest

import mcp_server as srv
from census import mcp_profiles, recipes, workflows_inference

CORE = srv.server_for_profile("core")
CORE_TOOLS = {t.name: t for t in CORE._tool_manager.list_tools()}


def _nested_params(schema):
    """Top-level parameters whose schema is (or contains) an object type."""
    def is_object(node):
        if not isinstance(node, dict):
            return False
        if "$ref" in node or node.get("type") == "object" or "additionalProperties" in node:
            return True
        if "items" in node and is_object(node["items"]):
            return True
        return any(is_object(n) for key in ("anyOf", "oneOf", "allOf") for n in node.get(key, []))

    return [name for name, node in schema.get("properties", {}).items() if is_object(node)]


class TestProfileSelection(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(mcp_profiles.resolve_profile([], {}), "full")
        self.assertEqual(mcp_profiles.resolve_profile([], {"CENSUS_MCP_PROFILE": "review"}), "review")
        self.assertEqual(mcp_profiles.resolve_profile(["--profile", "core"], {"CENSUS_MCP_PROFILE": "review"}), "core")
        self.assertEqual(mcp_profiles.resolve_profile(["--profile=CORE"], {}), "core")
        with self.assertRaises(ValueError):
            mcp_profiles.resolve_profile(["--profile", "tiny"], {})

    def test_full_is_unchanged(self):
        self.assertIs(srv.server_for_profile("full"), srv.mcp)
        self.assertIn("get_recipe", [t.name for t in srv.mcp._tool_manager.list_tools()])

    def test_core_registers_only_profile_tools(self):
        registered = {t.name for t in srv.mcp._tool_manager.list_tools()}
        expected = [n for n in mcp_profiles.PROFILES["core"] if n in registered]
        self.assertEqual(list(CORE_TOOLS), expected)
        self.assertLessEqual(len(CORE_TOOLS), 14)
        # Every profile name except run_manifest (workstream D) must exist.
        for profile in ("core", "review"):
            missing = set(mcp_profiles.PROFILES[profile]) - registered - {"run_manifest"}
            self.assertFalse(missing, f"{profile} names unknown tools: {missing}")

    def test_review_profile(self):
        review = srv.server_for_profile("review")
        names = {t.name for t in review._tool_manager.list_tools()}
        self.assertIn("record_crosswalk_review", names)
        self.assertNotIn("batch_download", names)


class TestCoreDesignRules(unittest.TestCase):
    def test_description_budget_and_example(self):
        for name, tool in CORE_TOOLS.items():
            with self.subTest(tool=name):
                desc = tool.description or ""
                self.assertLessEqual(len(desc), mcp_profiles.DESCRIPTION_BUDGET, f"{name}: {len(desc)} chars")
                example_lines = [ln for ln in desc.splitlines() if ln.strip().startswith("Example:")]
                self.assertEqual(len(example_lines), 1, f"{name}: needs exactly one 'Example:' line")
                self.assertIn(f"{name}(", example_lines[0])

    def test_flat_parameters(self):
        for name, tool in CORE_TOOLS.items():
            with self.subTest(tool=name):
                nested = set(_nested_params(tool.parameters))
                allowed = set(mcp_profiles.NESTED_PARAM_EXCEPTIONS.get(name, []))
                self.assertFalse(nested - allowed, f"{name}: nested params {nested - allowed}")

    def test_exceptions_are_real(self):
        for name, params in mcp_profiles.NESTED_PARAM_EXCEPTIONS.items():
            if name in CORE_TOOLS:
                self.assertTrue(set(params) <= set(_nested_params(CORE_TOOLS[name].parameters)))

    def test_recipe_name_is_enum(self):
        schema = CORE_TOOLS["get_recipe"].parameters["properties"]["name"]
        enums = [n.get("enum") for n in schema.get("anyOf", [schema]) if n.get("enum")]
        self.assertTrue(enums and set(enums[0]) == set(recipes.RECIPES))

    def test_inference_results_carry_summary_and_next_step(self):
        cases = {
            "trend_test": dict(years=[2013, 2018, 2023], estimates=[14.1, 13.0, 11.8], moes=[0.4, 0.4, 0.3]),
            "minimum_detectable_difference": dict(moe1=2.1, moe2=2.4),
            "fit_model": dict(input_path="does/not/exist.csv", formula="y ~ x"),
            "make_figure": dict(kind="nope", input_path="x.csv"),
            "build_analysis_frame": dict(input_path="does/not/exist.csv"),
            "run_manifest": dict(manifest_path="does/not/exist.json"),
        }
        inference_tools = {n for n in CORE_TOOLS if hasattr(workflows_inference, n)}
        self.assertTrue(inference_tools <= set(cases), f"add a case for {inference_tools - set(cases)}")
        for name, kwargs in cases.items():
            with self.subTest(tool=name):
                out = getattr(workflows_inference, name)(**kwargs)
                self.assertIn("summary", out)
                if out.get("success"):
                    self.assertIn("next_step", out)

    def test_core_wrapper_adds_guidance(self):
        ok = CORE_TOOLS["derive_estimate"].fn(operation="proportion", estimates=[12000, 80000],
                                               moes=[900, 1500], as_percent=True)
        self.assertEqual(list(ok)[:2], ["summary", "next_step"])
        self.assertAlmostEqual(ok["estimate"], 15.0)
        bad = CORE_TOOLS["derive_estimate"].fn(operation="mean", estimates=[1], moes=[1])
        self.assertFalse(bad["success"])
        self.assertTrue(bad["fix"])
        self.assertIn("next_step", bad)
        sig = CORE_TOOLS["calculate_significance"].fn(est1=14.2, moe1=1.1, est2=11.9, moe2=1.0)
        self.assertIn("significant", sig["summary"].lower())

    def test_list_results_become_dicts(self):
        out = CORE_TOOLS["search_tables"].fn(query="poverty", limit=3)
        self.assertIsInstance(out, dict)
        self.assertLessEqual(out["count"], 3)
        self.assertIn("next_step", out)


class TestRecipes(unittest.TestCase):
    def test_every_recipe_has_a_file_with_numbered_steps(self):
        for name in recipes.RECIPES:
            with self.subTest(recipe=name):
                path = os.path.join(recipes.RECIPE_DIR, f"{name}.md")
                self.assertTrue(os.path.exists(path))
                out = recipes.get_recipe(name)
                self.assertTrue(out["success"])
                self.assertIn("1. ", out["steps"])

    def test_list_and_unknown(self):
        self.assertEqual(len(recipes.get_recipe()["recipes"]), len(recipes.RECIPES))
        bad = recipes.get_recipe("nope")
        self.assertFalse(bad["success"])
        self.assertIn("fix", bad)


if __name__ == "__main__":
    unittest.main()
