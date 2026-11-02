# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP tool profiles (PLAN_ROUND_8 workstream J1/J2).

A profile is a named subset of the server's tools. Small local models do much
better with about ten short, flat tools than with the full set, so:

- ``core``: one tool per step of the standard workflow, with short
  descriptions (overrides below) and results that always carry ``summary`` and
  ``next_step`` (plus ``fix`` on failures).
- ``review``: concordance and rule review tools only, for reviewer agents.
- ``full``: every tool, descriptions unchanged (the default).

Select with ``python mcp_server.py --profile core`` or ``CENSUS_MCP_PROFILE=core``.
The library functions are not changed; overrides live here only.
"""

import functools
import inspect
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from census import errors

DEFAULT_PROFILE = "full"
ENV_VAR = "CENSUS_MCP_PROFILE"

PROFILES: Dict[str, Optional[List[str]]] = {
    "core": [
        "get_recipe",
        "search_tables",
        "describe_variable",
        "plan_extraction",
        "batch_download",
        "derive_estimate",
        "calculate_significance",
        "trend_test",
        "minimum_detectable_difference",
        "build_analysis_frame",
        "fit_model",
        "make_figure",
        "run_manifest",  # registered by workstream D; skipped while absent
    ],
    "review": [
        "get_research_protocol",
        "get_recipe",
        "describe_variable",
        "list_variables",
        "check_variable_concordance",
        "draft_crosswalk_override",
        "record_crosswalk_review",
        "validate_crosswalks",
        "draft_rule_override",
        "record_rule_review",
    ],
    "full": None,
}

# Core-profile parameters that are nested objects by necessity (J2 exceptions).
NESTED_PARAM_EXCEPTIONS: Dict[str, List[str]] = {
    # Optional list of {"name","op","num","den"} specs; the frame is usable without it.
    "build_analysis_frame": ["derive"],
}

DESCRIPTION_BUDGET = 700  # characters, enforced by tests/test_mcp_profiles.py

# Short descriptions for the core profile: one sentence, one line per key
# parameter, one example call. The full profile keeps the library docstrings.
DESCRIPTION_OVERRIDES: Dict[str, str] = {
    "get_recipe": (
        "Return numbered steps for a standard analysis. Call this first when unsure what to do.\n"
        "name: compare_counties | tract_trend | regression_sampling_error | small_area | preregistered; "
        "omit to list recipes.\n"
        "Example: get_recipe(name=\"compare_counties\")"
    ),
    "search_tables": (
        "Find ACS 5-year tables by code or keyword.\n"
        "query: keyword or code, e.g. 'poverty' or 'B17001'. limit: max results.\n"
        "Example: search_tables(query=\"median household income\")"
    ),
    "describe_variable": (
        "Show label, concept, table and universe of one variable code in one release.\n"
        "code: e.g. 'B19013_001E'. year: release year (default latest).\n"
        "Example: describe_variable(code=\"B17001_002E\", year=2023)"
    ),
    "plan_extraction": (
        "Dry run of batch_download: API calls, concordance issues, would_block. Downloads nothing.\n"
        "state_code: 2-digit FIPS. table_code: e.g. 'B17001'. start_year, end_year: releases.\n"
        "geo_level: us|state|county|tract|place|zcta|msa. variables: codes used. county_codes: 3-digit FIPS.\n"
        "Example: plan_extraction(state_code=\"48\", table_code=\"B17001\", start_year=2018, "
        "end_year=2023, geo_level=\"county\", variables=[\"B17001_001\", \"B17001_002\"])"
    ),
    "batch_download": (
        "Download a table across years and geographies to files; same arguments as plan_extraction.\n"
        "output_format: panel|both|parquet|excel. compute_reliability: add SE/CV columns.\n"
        "Returns output file paths, not the rows. Stops with CONCORDANCE_REVIEW_REQUIRED "
        "if codes changed meaning.\n"
        "Example: batch_download(state_code=\"48\", table_code=\"B17001\", start_year=2023, "
        "end_year=2023, geo_level=\"county\", county_codes=[\"453\"], output_format=\"panel\")"
    ),
    "derive_estimate": (
        "Compute a rate, ratio, sum, difference or percent change with its 90% MOE (Census formulas).\n"
        "operation: proportion (numerator is part of denominator) | ratio | sum | difference | "
        "percent_change | product.\n"
        "estimates, moes: [numerator, denominator] and their MOEs. as_percent: 0-100 scale.\n"
        "Example: derive_estimate(operation=\"proportion\", estimates=[12000, 80000], "
        "moes=[900, 1500], as_percent=true)"
    ),
    "calculate_significance": (
        "Z-test: do two ACS estimates differ significantly?\n"
        "est1, moe1, est2, moe2: estimates and 90% MOEs. confidence_level: 0.90 default.\n"
        "year1, year2: give both for the same place in two releases (overlap adjustment).\n"
        "Example: calculate_significance(est1=14.2, moe1=1.1, est2=11.9, moe2=1.0)"
    ),
    "trend_test": (
        "Test a linear trend across 3+ NON-overlapping 5-year releases (e.g. 2013, 2018, 2023).\n"
        "years, estimates, moes: equal-length lists. Overlapping releases are refused.\n"
        "Example: trend_test(years=[2013, 2018, 2023], estimates=[14.1, 13.0, 11.8], moes=[0.4, 0.4, 0.3])"
    ),
    "minimum_detectable_difference": (
        "Smallest difference two estimates with these MOEs can detect (planning).\n"
        "moe1, moe2: 90% MOEs (moe2 defaults to moe1). power: default 0.80.\n"
        "Example: minimum_detectable_difference(moe1=2.1, moe2=2.4) -> about 4.8"
    ),
    "build_analysis_frame": (
        "Make a model-ready file from a batch_download output: estimate, _se and _cv per variable.\n"
        "input_path: downloaded CSV/Parquet. variables: columns to keep. use_replicates, "
        "table_code, year: attach Census replicates for exact sampling error.\n"
        "Example: build_analysis_frame(input_path=\"downloads/B17001_panel.csv\", "
        "variables=[\"B17001_002E\"])"
    ),
    "fit_model": (
        "Fit a regression whose standard errors include ACS sampling error.\n"
        "input_path: analysis frame. formula: 'y ~ x1 + x2'. method: replicate | "
        "regression_calibration | simex | fay_herriot. model: ols|wls|binomial|poisson|negbin.\n"
        "replicates_path: from build_analysis_frame. se_col: outcome SE (fay_herriot).\n"
        "Example: fit_model(input_path=\"downloads/analysis_frame.parquet\", "
        "formula=\"poverty_rate ~ median_rent\")"
    ),
    "make_figure": (
        "Write a journal-ready figure (PDF, SVG, PNG) with values CSV and caption.\n"
        "kind: estimates | forest | trend. input_path: CSV/Parquet. label_col, estimate_col, "
        "moe_col, period_col: column names.\n"
        "Example: make_figure(kind=\"estimates\", input_path=\"rates.csv\", label_col=\"NAME\", "
        "estimate_col=\"rate\", moe_col=\"rate_moe\")"
    ),
}

# next_step for core tools whose results do not set one themselves.
NEXT_STEPS: Dict[str, str] = {
    "search_tables": "describe_variable on the codes you need, then plan_extraction",
    "describe_variable": "plan_extraction with these codes in variables=[...]",
    "plan_extraction": "if would_block is false, batch_download with the same arguments",
    "batch_download": "derive_estimate for rates, or build_analysis_frame for models",
    "derive_estimate": "calculate_significance to compare this estimate with another",
    "calculate_significance": "report the difference, its p_value and the verdict",
}

CORE_INSTRUCTIONS = """\
US Census ACS 5-year data tools. Every result has `summary` and `next_step`.
1. For any "how do I..." or multi-step task, call get_recipe FIRST and follow it
   (no arguments lists recipes).
2. Report every estimate with its 90% margin of error (MOE).
3. Rates: derive_estimate(operation='proportion') when the numerator is part of
   the denominator. Compare two estimates with calculate_significance.
4. Trends need non-overlapping releases (e.g. 2013, 2018, 2023): trend_test.
5. Before batch_download, call plan_extraction; stop if would_block is true.
6. On failure read error_code and fix; do not guess numbers.
"""

REVIEW_INSTRUCTIONS = """\
ACS concordance review tools. Call get_research_protocol first. Drafters use
draft_crosswalk_override / draft_rule_override; a DIFFERENT identity reviews with
record_crosswalk_review / record_rule_review. Never review your own draft.
"""


def resolve_profile(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> str:
    """Profile from ``--profile NAME`` / ``--profile=NAME`` in argv, else the
    environment variable, else ``full``. Raises ValueError for unknown names."""
    env = os.environ if env is None else env
    name = env.get(ENV_VAR) or DEFAULT_PROFILE
    args = list(argv or [])
    for i, arg in enumerate(args):
        if arg == "--profile" and i + 1 < len(args):
            name = args[i + 1]
        elif arg.startswith("--profile="):
            name = arg.split("=", 1)[1]
    name = name.strip().lower()
    if name not in PROFILES:
        raise ValueError(f"Unknown MCP profile '{name}'. Choose from {sorted(PROFILES)}.")
    return name


def tools_for(profile: str, available: Sequence[str]) -> List[str]:
    """Tool names in a profile that are actually registered, in profile order."""
    wanted = PROFILES[profile]
    if wanted is None:
        return list(available)
    have = set(available)
    return [name for name in wanted if name in have]


def _summary_for(name: str, out: Dict[str, Any]) -> str:
    if out.get("success") is False:
        return str(out.get("error") or "The call failed.")
    if name == "derive_estimate" and "estimate" in out:
        return f"{out.get('method')} estimate {out['estimate']:.4g} with 90% MOE {out.get('moe', float('nan')):.4g}."
    if name == "calculate_significance" and "verdict" in out:
        return f"Difference {out.get('difference', 0):.4g} (p={out.get('p_value', 0):.3g}). {out['verdict']}"
    if name == "plan_extraction" and "would_block" in out:
        return f"Plan ready; would_block={out['would_block']}."
    if "count" in out and "results" in out:
        return f"{out['count']} result(s)."
    return "Done."


def with_guidance(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool so dict results carry ``summary`` and ``next_step`` (and
    ``fix`` with the plain-language meaning of ``error_code`` on failure).
    List results are wrapped as ``{"results": [...]}``. Signature is kept."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        out = fn(*args, **kwargs)
        if isinstance(out, list):
            out = {"success": True, "count": len(out), "results": out}
        if isinstance(out, dict) and isinstance(out.get("summary"), dict):
            # e.g. plan/batch results carry a concordance summary dict under "summary"
            out = {**{k: v for k, v in out.items() if k != "summary"}, "concordance_summary": out["summary"]}
        if not isinstance(out, dict):
            return out
        guided = dict(out)
        guided.setdefault("summary", _summary_for(name, out))
        if out.get("success") is False:
            guided.setdefault("fix", errors.ERROR_CODES.get(str(out.get("error_code")), ""))
            guided.setdefault("next_step", "fix the arguments as described in `fix` and call again, "
                                           "or get_recipe for the standard steps")
        else:
            guided.setdefault("next_step", NEXT_STEPS.get(name, "get_recipe for the next step"))
        return {"summary": guided.pop("summary"), "next_step": guided.pop("next_step"), **guided}

    # A list-returning tool now returns a dict; keep the output schema honest.
    wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=Dict[str, Any])  # type: ignore[attr-defined]
    return wrapper


def build_profile_server(base: Any, profile: str) -> Any:
    """Return a FastMCP server exposing only ``profile``'s tools.

    ``full`` returns ``base`` unchanged. Other profiles get a new FastMCP with
    shorter instructions; core tools use DESCRIPTION_OVERRIDES and
    ``with_guidance`` results."""
    if PROFILES[profile] is None:
        return base
    from mcp.server.fastmcp import FastMCP

    manager = base._tool_manager
    available = [t.name for t in manager.list_tools()]
    instructions = CORE_INSTRUCTIONS if profile == "core" else REVIEW_INSTRUCTIONS
    server = FastMCP(f"{base.name} [{profile}]", instructions=instructions)
    for name in tools_for(profile, available):
        tool = manager.get_tool(name)
        if profile == "core":
            fn = with_guidance(name, tool.fn)
            description = DESCRIPTION_OVERRIDES.get(name, tool.description)
        else:
            fn, description = tool.fn, tool.description
        server.add_tool(fn, name=name, description=description)
    return server
