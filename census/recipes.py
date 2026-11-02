# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workflow recipes (PLAN_ROUND_8 J3): short numbered steps an agent can follow
instead of planning from scratch. Recipes are Markdown files in docs/recipes/."""

import os
from typing import Any, Dict, Literal, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPE_DIR = os.path.join(ROOT, "docs", "recipes")

RECIPES: Dict[str, str] = {
    "compare_counties": "Compare a rate between two counties in one release",
    "tract_trend": "Trend in a tract or county rate using non-overlapping releases",
    "regression_sampling_error": "Regression whose SEs include ACS sampling error",
    "small_area": "Small-area (Fay-Herriot) estimates for noisy tracts or counties",
    "preregistered": "Pre-registered (manifest) confirmatory analysis",
}

RecipeName = Literal[
    "compare_counties", "tract_trend", "regression_sampling_error", "small_area", "preregistered"
]


def get_recipe(name: Optional[RecipeName] = None) -> Dict[str, Any]:
    """Return numbered steps for a standard analysis; omit name to list recipes.

    Args:
        name: compare_counties, tract_trend, regression_sampling_error,
            small_area, or preregistered.

    Example: get_recipe(name="compare_counties")
    """
    if not name:
        return {
            "success": True,
            "summary": f"{len(RECIPES)} recipes available.",
            "next_step": "get_recipe(name=...) with one of the names in recipes",
            "recipes": [{"name": k, "description": v} for k, v in RECIPES.items()],
        }
    key = str(name).strip().lower()
    if key not in RECIPES:
        return {
            "success": False,
            "error_code": "INVALID_INPUT",
            "error": f"Unknown recipe '{name}'.",
            "summary": f"Unknown recipe '{name}'.",
            "fix": f"Use one of: {', '.join(RECIPES)}.",
            "next_step": "get_recipe() to list recipes",
        }
    with open(os.path.join(RECIPE_DIR, f"{key}.md"), encoding="utf-8") as f:
        text = f.read()
    return {
        "success": True,
        "summary": RECIPES[key] + ".",
        "next_step": "follow step 1 of the recipe",
        "name": key,
        "steps": text,
    }
