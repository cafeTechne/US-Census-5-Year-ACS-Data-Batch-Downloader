# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-registered analysis manifests (PLAN_ROUND_8 workstream D).

A manifest is a JSON document that fixes an analysis before any data are
pulled. Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "title": "Income and poverty in NY tracts",
      "authors": ["human:jdoe", "agent:claude"],           # identity convention of overrides
      "created": "2026-11-02T12:00:00Z",                    # ISO 8601
      "releases": [2019],                                   # ACS 5-year end years
      "geography": {"level": "tract", "state": "36", "codes": ["061"]},
                   # or several states, stacked: {"level": "county", "states": ["26", "36"]}
      "variables": [                                        # YEAR:CODE or concept IDs
        {"name": "income", "ref": "2019:B19013_001"},
        {"name": "pop", "ref": "c_0123456789ab", "table": "B01003"}
      ],
      "aggregation": {"mapping": {"<GEOID>": "<group>"}},    # optional
      "derivations": [{"name": "rate", "op": "proportion", "num": "a", "den": "b"}],
      "adjustments": [{"type": "rpp", "variables": ["income"],   # optional; after exclusions
                       "rpp_year": 2019, "areas": "county_metro" | "state"}],
      "exclusions": [{"rule": "cv_screen", "threshold": 30, "variables": ["income"],
                      "action": "flag" | "exclude"},
                     {"rule": "geoid_exclude", "geoids": ["..."]}],
      "missing": "complete_case",
      "confidence_level": 0.90,
      "correction": {"method": "holm", "family": "all" | "by_family"},
      "analyses": [{"id": "m1", "method": "fit_replicate", "formula": "income ~ pop",
                    "args": {"model": "ols"}}],
      "hypotheses": [{"id": "H1", "statement": "...", "analysis": "m1",
                      "statistic": "coefficient", "term": "pop",
                      "direction": "two-sided" | "greater" | "less", "family": "primary"}],
      "data_source": {"synthetic": true,                    # optional; default: real Census API data
                      "description": "...", "generator": "..."}
    }

``geography`` takes ``state`` (one state, or ``"all"``) or ``states`` (a list
of 2-digit FIPS codes), never both. With ``states`` each table is extracted
per state and the states are stacked into one frame; ``codes`` are then not
allowed (county codes are per state). Pooled models get no automatic state
term: a cross-state formula states it explicitly, e.g. ``C(GEOID.str[:2])``.

``adjustments`` (optional) are pre-registered transformations applied to the
analysis frame after derivations and exclusions (so a pre-registered exclusion
can remove a unit without an RPP area; a remaining unit without one is a
blocking ``adjustment_failed``). ``"rpp"`` divides
dollar variables (``variables``: manifest variable or derivation names), their
SEs and any supplied replicates by the BEA Regional Price Parity / 100 of each
row's area (``areas``: ``"county_metro"``, the default for county geographies,
or ``"state"``), with the same code as the ``place_adjust_frame`` tool
(:func:`census.workflows_microdata.place_adjust`). ``rpp_year`` is required so the
manifest hash fixes the price year (:func:`create_manifest` fills in the latest
year in the RPP table when it is omitted, before hashing); the year used, every
RPP area and the RPP table's SHA-256 (bundled checksum, or the custom table's path and hash) are
recorded in ``run.json`` and ``bundle.json`` (``adjustments``). An RPP year
other than the release end year is a note, not a deviation. One release only.

When replicates are supplied and the manifest has derivations, each
derivation absent from the replicate frame is evaluated on the replicates of
its inputs (replicate rows named by manifest variable name or ACS code), and
its SE becomes the exact replicate SE where its inputs have replicates.
``replicates.derived_on_replicates`` lists only derivations that received a
replicate SE, with per-derivation counts (``derived_on_replicates_rows``:
rows_replicate of rows in the analysed frame); a failure of that step is a
blocking ``replicates_incomplete`` deviation.

``data_source`` (optional) *declares* where the input data come from. The
labels follow where the data actually came from (:func:`observed_data_source`):
data are synthetic when the client that served them carries a true
``synthetic`` attribute or its cache was marked with
``QueryCache.mark_synthetic``; otherwise they are real Census Data API data.
A declaration that disagrees with the observed source records a blocking
``data_source_mismatch`` deviation, and the citation and methods text follow
the observed source (real Census data are never called synthetic; synthetic
data never claim retrieval from the Census Data API).

Run status (``analysis_status`` in results, ``results/run.json``,
``bundle.json`` provenance and ``methods.md``):

- ``"confirmatory"`` (:data:`STATUS_CONFIRMATORY`): the pre-registered
  analysis ran as registered, with no blocking deviation.
- ``"confirmatory_incomplete"`` (:data:`STATUS_CONFIRMATORY_INCOMPLETE`): at
  least one blocking deviation (:data:`BLOCKING_DEVIATIONS`, e.g. a hypothesis
  not tested, an analysis failed, replicates incomplete, an extraction
  failure); the pre-registered analysis did not run as registered and its
  results are not confirmatory.
- ``"exploratory"``: anything run outside a manifest (:func:`label_exploratory`).

Supported analysis methods are :data:`ANALYSIS_METHODS`; hypothesis
statistics are :data:`STATISTICS`. :func:`create_manifest` validates (every
variable resolves in the offline metadata archive through the concordance,
every method is supported) and returns the SHA-256 of the canonical JSON
(:func:`manifest_hash`). :func:`run_manifest` executes it exactly and writes
the hash into every output; deviations are recorded, never substituted
silently. Anything run outside a manifest should be tagged with
:func:`label_exploratory`.
"""

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import numpy as np
import pandas as pd

from .results import IncompleteReplicatesError, ModelResult, interval_description

SCHEMA_VERSION = 1
AUTHOR_RE = re.compile(r"^(human|agent):\S+$")
CONCEPT_RE = re.compile(r"^c_[0-9a-f]{12}$")
YEAR_CODE_RE = re.compile(r"^(\d{4}):([A-Z]+\d+[A-Z]*_\d+[A-Z]*)$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DIRECTIONS = ("two-sided", "greater", "less")
STATUS_CONFIRMATORY = "confirmatory"
STATUS_CONFIRMATORY_INCOMPLETE = "confirmatory_incomplete"
INCOMPLETE_STATUS_TEXT = "pre-registered analysis did not run as registered; results are not confirmatory"
# deviations meaning the pre-registered analysis did not run as registered
BLOCKING_DEVIATIONS = ("blocked_variable", "extraction_failed", "variable_missing_from_extraction",
                       "aggregation_failed", "analysis_frame_failed", "analysis_not_run",
                       "replicates_unavailable", "analysis_failed", "replicates_incomplete",
                       "hypothesis_not_tested", "failed_calls", "data_source_mismatch", "adjustment_failed")
DATA_SOURCE_KEYS = ("synthetic", "description", "generator")
STATUS_EXPLORATORY = "exploratory"

# method -> (required args, statistics it supports)
ANALYSIS_METHODS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "fit_replicate": (("formula",), ("coefficient",)),
    "regression_calibration": (("formula", "error_covariates"), ("coefficient",)),
    "simex": (("formula", "error_covariates"), ("coefficient",)),
    "fh_regression": (("formula",), ("coefficient",)),
    "fay_herriot": (("formula",), ("coefficient",)),
    "correlation_matrix": (("variables",), ("correlation",)),
    "linear_trend_test": (("variable", "geoid"), ("trend_slope",)),
    "batch_z_test": (("variable_1", "variable_2"), ("z_difference",)),
}
STATISTICS = ("coefficient", "correlation", "trend_slope", "z_difference")
EXCLUSION_RULES = ("cv_screen", "geoid_exclude")
ADJUSTMENT_TYPES = ("rpp",)
RPP_ADJUSTMENT_KEYS = ("type", "variables", "rpp_year", "areas")
# geography levels whose GEOIDs start with the state FIPS (state RPP areas; usable with
# geography.states). ZCTA GEOIDs are not state-prefixed and ZCTAs can cross state lines.
_STATE_PREFIXED_LEVELS = ("state", "county", "tract", "block group", "place")
FIPS2_RE = re.compile(r"^\d{2}$")
FORMULA_FUNCTIONS = {"C", "I", "np", "log", "exp", "sqrt", "center", "standardize", "scale", "Q"}

REFERENCES = {
    "acs": "U.S. Census Bureau (2020). Understanding and Using American Community Survey Data: "
           "What All Data Users Need to Know (ACS General Handbook), ch. 7-8.",
    "vre": "U.S. Census Bureau. Variance Replicate Estimate Tables Documentation. "
           "https://www.census.gov/programs-surveys/acs/data/variance-tables.html",
    "design": "U.S. Census Bureau (2014). American Community Survey Design and Methodology, ch. 12-13.",
    "testing": "U.S. Census Bureau (2025). Instructions for Applying Statistical Testing to ACS Data.",
    "carroll": "Carroll, R.J., Ruppert, D., Stefanski, L.A. & Crainiceanu, C.M. (2006). Measurement "
               "Error in Nonlinear Models: A Modern Perspective, 2nd ed. Chapman & Hall/CRC.",
    "simex": "Cook, J.R. & Stefanski, L.A. (1994). JASA 89, 1314-1328; Stefanski, L.A. & Cook, J.R. "
             "(1995). JASA 90, 1247-1256; Devanarayan, V. & Stefanski, L.A. (2002). Stat. Prob. "
             "Letters 59, 219-225.",
    "fisher": "Fisher, R.A. (1921). On the probable error of a coefficient of correlation. Metron 1, 3-32.",
    "holm": "Holm, S. (1979). Scand. J. Statist. 6, 65-70.",
    "bh": "Benjamini, Y. & Hochberg, Y. (1995). JRSS-B 57, 289-300.",
    "by": "Benjamini, Y. & Yekutieli, D. (2001). Ann. Statist. 29, 1165-1188.",
    "bonferroni": "Dunn, O.J. (1961). JASA 56, 52-64.",
    "statsmodels": "Seabold, S. & Perktold, J. (2010). statsmodels: Econometric and statistical "
                   "modeling with Python. Proc. 9th Python in Science Conf.",
}


# ---------------------------------------------------------------------------
# Hashing, loading
# ---------------------------------------------------------------------------


def canonical_json(manifest: Mapping[str, Any]) -> str:
    """Canonical serialization: sorted keys, no whitespace, UTF-8 text."""
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """SHA-256 (hex) of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def load_manifest(path_or_dict: Union[str, Path, Mapping[str, Any]]) -> Dict[str, Any]:
    if isinstance(path_or_dict, Mapping):
        return copy.deepcopy(dict(path_or_dict))
    with open(path_or_dict, encoding="utf-8") as f:
        return json.load(f)


def _table_of(var: Mapping[str, Any]) -> Optional[str]:
    if var.get("table"):
        return str(var["table"]).upper()
    m = YEAR_CODE_RE.match(str(var.get("ref", "")))
    return m.group(2).split("_")[0] if m else None


def _dataset_for(table: str) -> str:
    from .api import infer_dataset
    return infer_dataset(table)


def manifest_states(geography: Mapping[str, Any]) -> List[Optional[str]]:
    """The geography's state selections: ``states`` as given, else ``[state]``
    (``[None]`` for levels that need no state)."""
    if geography.get("states") is not None:
        return [str(s) for s in geography["states"]]
    return [geography.get("state")]


def manifest_entities(geography: Mapping[str, Any], years: Optional[List[int]] = None) -> List[Any]:
    """Request entities for a manifest geography: one state's entities, or each
    listed state's entities in order (extracted per state and stacked)."""
    from .pipeline import build_entities
    entities: List[Any] = []
    for st in manifest_states(geography):
        entities.extend(build_entities(geography["level"], st, geography.get("codes"), years=years))
    return entities


def _check_geography(geo: Mapping[str, Any], errors: List[str]) -> None:
    from .pipeline import expand_states, is_nationwide, normalize_geo_level
    if not isinstance(geo, Mapping) or not geo.get("level"):
        errors.append("geography.level is required")
        return
    state = geo.get("state")
    if isinstance(state, str) and not is_nationwide(state):
        bad = [p for p in state.split(",") if not FIPS2_RE.match(p.strip())]
        if bad:
            errors.append(f"geography.state: {bad[0]!r} must be a state FIPS code of exactly 2 digits "
                          "(e.g. \"06\", not \"6\"), or \"all\"")
            return
    states = geo.get("states")
    if states is not None:
        if "state" in geo:
            errors.append("geography: give either 'state' or 'states', not both")
            return
        if not isinstance(states, list) or not states or not all(isinstance(s, str) for s in states):
            errors.append("geography.states must be a non-empty list of 2-digit state FIPS strings, "
                          "e.g. [\"26\", \"36\"]")
            return
        for s in states:
            if is_nationwide(s) or "," in s:
                errors.append(f"geography.states: {s!r} must be one 2-digit state FIPS code (for every "
                              "state use \"state\": \"all\")")
                return
            if not FIPS2_RE.match(s):
                errors.append(f"geography.states: {s!r} must be a state FIPS code of exactly 2 digits "
                              "(e.g. \"06\", not \"6\")")
                return
            try:
                expand_states(s)
            except ValueError as exc:
                errors.append(f"geography.states: {exc}")
                return
        if len(set(states)) != len(states):
            errors.append("geography.states lists a state more than once")
            return
        if geo.get("codes"):
            errors.append("geography: 'codes' cannot be combined with 'states' (codes are per state); "
                          "list GEOIDs in a geoid_exclude exclusion or use one state per manifest")
            return
        try:
            level = normalize_geo_level(geo["level"])
        except Exception:  # noqa: BLE001 - reported by build_entities below
            level = geo["level"]
        if level == "zcta":
            errors.append("geography.states does not apply to level 'zcta': ZCTA GEOIDs are not state-prefixed "
                          "and ZCTAs can cross state lines, so they cannot be extracted per state and stacked")
            return
        if level not in _STATE_PREFIXED_LEVELS:
            errors.append(f"geography.states does not apply to level {geo['level']!r} (national geography)")
            return
    try:
        manifest_entities(geo)
    except Exception as exc:
        errors.append(f"geography: {exc}")


def default_rpp_areas(level: Optional[str]) -> str:
    """Default RPP ``areas`` of an adjustment for a (normalized) geography level: the
    county's metro/nonmetro area for counties, else the state (validation and runs)."""
    return "county_metro" if level == "county" else "state"


def latest_rpp_year() -> Optional[int]:
    """Latest year in the active RPP table (filled in by create_manifest when ``rpp_year`` is omitted), or None."""
    from . import rpp
    years = rpp._years()
    return years[-1] if years else None


def _check_adjustments(manifest: Mapping[str, Any], names: List[str], errors: List[str]) -> None:
    adjustments = manifest.get("adjustments")
    if adjustments is None:
        return
    if not isinstance(adjustments, list):
        errors.append("adjustments must be a list of adjustment steps")
        return
    from .pipeline import normalize_geo_level
    geo = manifest.get("geography") or {}
    try:
        level = normalize_geo_level(geo.get("level", ""))
    except Exception:  # noqa: BLE001 - geography errors are reported by _check_geography
        level = geo.get("level")
    if adjustments and len(manifest.get("releases") or []) != 1:
        errors.append("adjustments: an RPP adjustment needs exactly one release (dollars of one end year)")
    if adjustments and manifest.get("aggregation") is not None:
        errors.append("adjustments cannot be combined with aggregation (RPP areas are assigned per GEOID)")
    adjusted: List[str] = []
    for i, adj in enumerate(adjustments):
        if not isinstance(adj, Mapping):
            errors.append(f"adjustments[{i}] must be an object")
            continue
        if adj.get("type") not in ADJUSTMENT_TYPES:
            errors.append(f"adjustments[{i}]: type must be one of {ADJUSTMENT_TYPES}")
            continue
        for k in sorted(set(adj) - set(RPP_ADJUSTMENT_KEYS)):
            errors.append(f"adjustments[{i}]: unknown key {k!r}; allowed {RPP_ADJUSTMENT_KEYS}")
        vars_ = adj.get("variables")
        if not isinstance(vars_, list) or not vars_:
            errors.append(f"adjustments[{i}]: variables must be a non-empty list of manifest variable names")
            vars_ = []
        for v in vars_:
            if str(v) not in names:
                errors.append(f"adjustments[{i}]: unknown variable {v!r}")
            elif str(v) in adjusted:
                errors.append(f"adjustments[{i}]: {v!r} is adjusted twice")
            adjusted.append(str(v))
        areas = adj.get("areas", default_rpp_areas(level))
        if areas not in ("county_metro", "state"):
            errors.append(f"adjustments[{i}]: areas must be 'county_metro' or 'state'")
        elif areas == "county_metro" and level != "county":
            errors.append(f"adjustments[{i}]: areas 'county_metro' needs geography level 'county'")
        elif areas == "state" and level not in _STATE_PREFIXED_LEVELS:
            errors.append(f"adjustments[{i}]: areas 'state' needs a state-based geography level "
                          f"{_STATE_PREFIXED_LEVELS}")
        year = adj.get("rpp_year")
        try:
            from . import rpp
            years = rpp._years()
        except Exception as exc:  # noqa: BLE001 - e.g. a bundled-data integrity failure
            errors.append(f"adjustments[{i}]: RPP table unreadable ({type(exc).__name__}: {exc})")
            continue
        if not years:
            errors.append(f"adjustments[{i}]: no RPP data available")
        elif year is None:
            errors.append(f"adjustments[{i}]: rpp_year is required (it fixes the price level in the "
                          f"pre-registration); the latest year in the RPP table is {years[-1]}. "
                          "create_manifest fills it in when omitted")
        elif not isinstance(year, int) or isinstance(year, bool) or year not in years:
            errors.append(f"adjustments[{i}]: rpp_year must be an integer year in the RPP table "
                          f"({years[0]}-{years[-1]})")


def tables_and_refs(manifest: Mapping[str, Any]) -> Dict[str, List[str]]:
    """table code -> variable refs (in manifest order)."""
    out: Dict[str, List[str]] = {}
    for v in manifest.get("variables", []):
        t = _table_of(v)
        if t:
            out.setdefault(t, []).append(str(v["ref"]))
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_SPEC_KEYS = ("formula", "error_covariates", "variables", "variable", "geoid", "variable_1", "variable_2")


def _normalize_analysis(a: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the analysis with spec keys (formula, variables, ...) lifted from
    ``args`` to the top level, so either placement works the same way.
    Duplicates (a key both top-level and in args) are a validation error."""
    out = dict(a)
    args = dict(a.get("args") or {})
    for k in _SPEC_KEYS:
        if k in args and k not in out:
            out[k] = args.pop(k)
        elif k in args:
            args.pop(k)
    out["args"] = args
    return out


def _duplicate_spec_keys(a: Mapping[str, Any]) -> List[str]:
    args = a.get("args") or {}
    return [k for k in _SPEC_KEYS if k in a and k in args]


def _formula_names(formula: str) -> List[str]:
    """Identifiers in a patsy formula that are not function calls."""
    names = []
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_.]*", str(formula)):
        tail = str(formula)[m.end():].lstrip()
        if tail.startswith("(") or m.group(0) in FORMULA_FUNCTIONS or "." in m.group(0):
            continue
        names.append(m.group(0))
    return names


def _check_variables(manifest: Mapping[str, Any], errors: List[str], resolve: bool, client: Any) -> None:
    from .metadata import archived_vintages, vintage_variables
    releases = [int(r) for r in manifest.get("releases", []) if str(r).isdigit()]
    variables = manifest.get("variables") or []
    if not variables:
        errors.append("variables: at least one variable is required")
        return
    by_table: Dict[str, List[str]] = {}
    for i, v in enumerate(variables):
        if not isinstance(v, Mapping) or "name" not in v or "ref" not in v:
            errors.append(f"variables[{i}]: needs 'name' and 'ref'")
            continue
        ref = str(v["ref"])
        m = YEAR_CODE_RE.match(ref)
        if not m and not CONCEPT_RE.match(ref):
            errors.append(f"variables[{i}] {ref!r}: ref must be YEAR:CODE or a concept ID (c_ + 12 hex)")
            continue
        table = _table_of(v)
        if table is None:
            errors.append(f"variables[{i}] {ref!r}: a concept ID needs 'table'")
            continue
        dataset = _dataset_for(table)
        if m:
            year, code = int(m.group(1)), m.group(2)
            if year not in releases:
                errors.append(f"variables[{i}] {ref!r}: year {year} is not among releases {releases}")
            if resolve:
                if year not in archived_vintages(dataset):
                    errors.append(f"variables[{i}] {ref!r}: {dataset} {year} not in the offline metadata archive")
                    continue
                known, _ = vintage_variables(dataset, year)
                if not any(k == code or k.startswith(code) and k[len(code):] in ("E", "PE") for k in known):
                    errors.append(f"variables[{i}] {ref!r}: {code} does not exist in {dataset} {year}")
                    continue
        by_table.setdefault(table, []).append(ref)
    if not resolve:
        return
    from .concordance import build_concordance
    for table, refs in by_table.items():
        dataset = _dataset_for(table)
        missing = [y for y in releases if y not in archived_vintages(dataset)]
        if missing:
            errors.append(f"{table}: releases {missing} not in the offline metadata archive for {dataset}")
            continue
        try:
            conc = build_concordance(table, dataset, releases, client=client)
            _, unknown = conc.resolve_selection(refs)
        except Exception as exc:  # concordance build failures are validation errors
            errors.append(f"{table}: concordance could not be built ({exc})")
            continue
        for ref in unknown:
            errors.append(f"variable {ref!r} does not resolve in the {table} concordance for {releases}")


def validate_manifest(manifest: Mapping[str, Any], resolve: bool = True, client: Any = None) -> List[str]:
    """Return a list of errors (empty when the manifest is valid).

    ``resolve``: check every variable against the offline metadata archive
    and the concordance (no network when the archive covers the releases).
    """
    errors: List[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(manifest.get("title", "")).strip():
        errors.append("title is required")
    authors = manifest.get("authors") or []
    if not authors:
        errors.append("authors: at least one author ('human:<id>' or 'agent:<id>') is required")
    for a in authors:
        if not AUTHOR_RE.match(str(a)):
            errors.append(f"author {a!r} must look like 'human:<id>' or 'agent:<id>'")
    try:
        datetime.fromisoformat(str(manifest.get("created", "")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("created must be an ISO 8601 timestamp")
    releases = manifest.get("releases") or []
    if not releases or not all(isinstance(r, int) for r in releases):
        errors.append("releases must be a non-empty list of ACS 5-year end years (integers)")
    _check_geography(manifest.get("geography") or {}, errors)
    level = manifest.get("confidence_level", 0.90)
    if not isinstance(level, (int, float)) or not 0 < float(level) < 1:
        errors.append("confidence_level must be in (0, 1), e.g. 0.90")
    corr = manifest.get("correction") or {}
    try:
        from .multitest import _method
        _method(corr.get("method", "holm"))
    except ValueError as exc:
        errors.append(f"correction: {exc}")
    if corr.get("family", "all") not in ("all", "by_family"):
        errors.append("correction.family must be 'all' or 'by_family'")
    ds = manifest.get("data_source")
    if ds is not None:
        if not isinstance(ds, Mapping):
            errors.append("data_source must be an object {synthetic, description, generator}")
        else:
            if not isinstance(ds.get("synthetic"), bool):
                errors.append("data_source.synthetic is required and must be true or false")
            for k in ("description", "generator"):
                if k in ds and not isinstance(ds[k], str):
                    errors.append(f"data_source.{k} must be a string")
            for k in sorted(set(ds) - set(DATA_SOURCE_KEYS)):
                errors.append(f"data_source: unknown key {k!r}; allowed {DATA_SOURCE_KEYS}")
    if manifest.get("missing", "complete_case") != "complete_case":
        errors.append("missing: only 'complete_case' is supported")

    _check_variables(manifest, errors, resolve, client)
    names = [str(v.get("name")) for v in manifest.get("variables", []) if isinstance(v, Mapping)]
    for d in manifest.get("derivations") or []:
        names.append(str(d.get("name")))
        for part in ("num", "den"):
            refs = d.get(part)
            for r in (refs if isinstance(refs, list) else [refs] if refs is not None else []):
                if str(r) not in names:
                    errors.append(f"derivation {d.get('name')!r}: {part} {r!r} is not an earlier variable name")
    for n in names:
        if not NAME_RE.match(n):
            errors.append(f"name {n!r} must be a Python identifier (used in formulas)")
    if len(set(names)) != len(names):
        errors.append("variable/derivation names must be unique")
    agg = manifest.get("aggregation")
    if agg is not None and not isinstance(agg.get("mapping"), Mapping):
        errors.append("aggregation.mapping must be an object GEOID -> group id")
    _check_adjustments(manifest, names, errors)

    for i, ex in enumerate(manifest.get("exclusions") or []):
        if ex.get("rule") not in EXCLUSION_RULES:
            errors.append(f"exclusions[{i}]: rule must be one of {EXCLUSION_RULES}")
        if ex.get("rule") == "cv_screen" and ex.get("action", "flag") not in ("flag", "exclude"):
            errors.append(f"exclusions[{i}]: action must be 'flag' or 'exclude'")
        for v in ex.get("variables") or []:
            if v not in names:
                errors.append(f"exclusions[{i}]: unknown variable {v!r}")

    analyses = manifest.get("analyses") or []
    if not analyses:
        errors.append("analyses: at least one analysis is required")
    ids = {}
    for i, a in enumerate(analyses):
        for k in _duplicate_spec_keys(a):
            errors.append(f"analyses[{i}]: {k!r} given both at the top level and in args; give it once")
        a = _normalize_analysis(a)
        method = a.get("method")
        if method not in ANALYSIS_METHODS:
            errors.append(f"analyses[{i}]: unsupported method {method!r}; supported {sorted(ANALYSIS_METHODS)}")
            continue
        if a.get("id") in ids or not a.get("id"):
            errors.append(f"analyses[{i}]: id must be present and unique")
        ids[a.get("id")] = method
        for req in ANALYSIS_METHODS[method][0]:
            if req not in a:
                errors.append(f"analyses[{i}] ({method}): {req!r} is required")
        used = _formula_names(a["formula"]) if a.get("formula") else []
        for key in ("variables", "error_covariates"):
            used += list(a.get(key) or [])
        for key in ("variable", "variable_1", "variable_2"):
            if a.get(key):
                used.append(a[key])
        for u in used:
            if u not in names:
                errors.append(f"analyses[{i}] ({a.get('id')}): {u!r} is not a manifest variable")
    hyps = manifest.get("hypotheses") or []
    if not hyps:
        errors.append("hypotheses: at least one pre-registered hypothesis is required")
    hids = set()
    for i, h in enumerate(hyps):
        if not h.get("id") or h["id"] in hids:
            errors.append(f"hypotheses[{i}]: id must be present and unique")
        hids.add(h.get("id"))
        if h.get("direction", "two-sided") not in DIRECTIONS:
            errors.append(f"hypotheses[{i}]: direction must be one of {DIRECTIONS}")
        method = ids.get(h.get("analysis"))
        if method is None:
            errors.append(f"hypotheses[{i}]: analysis {h.get('analysis')!r} is not defined")
            continue
        if h.get("statistic") not in ANALYSIS_METHODS[method][1]:
            errors.append(f"hypotheses[{i}]: statistic {h.get('statistic')!r} not available from {method} "
                          f"(supported: {ANALYSIS_METHODS[method][1]})")
        need = {"coefficient": ("term",), "correlation": ("variables",), "trend_slope": (),
                "z_difference": ("geoid",)}.get(h.get("statistic"), ())
        for k in need:
            if k not in h:
                errors.append(f"hypotheses[{i}]: {k!r} is required for statistic {h.get('statistic')!r}")
    return errors


def create_manifest(
    path_or_dict: Union[str, Path, Mapping[str, Any]], path: Optional[Union[str, Path]] = None,
    client: Any = None, resolve: bool = True,
) -> Dict[str, Any]:
    """Validate a manifest and record its SHA-256 before any data are pulled.

    When valid and ``path`` is given (or the input was a dict and ``path``
    is given) the manifest is written there as pretty JSON. Commit the file
    to the repository to fix the pre-registration (PLAN_ROUND_8 decision 3).

    An RPP adjustment without ``rpp_year`` gets the latest year in the active RPP
    table before validation and hashing, so the hash fixes the applied year; the
    result says so (``filled``, ``notes``) and the completed manifest is written to
    ``path``, or back to the input file when no ``path`` is given (a dict input
    without ``path`` gets it in ``manifest``).
    Returns ``{"hash", "path", "errors", "valid", "notes", "filled"}`` (and
    ``"manifest"`` when something was filled in).
    """
    manifest = load_manifest(path_or_dict)
    filled: List[Dict[str, Any]] = []
    adjustments = manifest.get("adjustments")
    if isinstance(adjustments, list):
        year = None
        for i, adj in enumerate(adjustments):
            if isinstance(adj, dict) and adj.get("type") == "rpp" and adj.get("rpp_year") is None:
                try:
                    year = year or latest_rpp_year()
                except Exception:  # noqa: BLE001 - an unreadable RPP table is reported by validation
                    year = None
                if year is not None:
                    adj["rpp_year"] = int(year)
                    filled.append({"adjustment": i, "rpp_year": int(year)})
    errors = validate_manifest(manifest, resolve=resolve, client=client)
    out_path = str(path) if path else (str(path_or_dict) if not isinstance(path_or_dict, Mapping) else None)
    write_to = path or (out_path if filled else None)
    if not errors and write_to:
        Path(write_to).parent.mkdir(parents=True, exist_ok=True)
        Path(write_to).write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                  encoding="utf-8", newline="\n")
    notes = [f"adjustments[{f['adjustment']}]: rpp_year was omitted; filled in with {f['rpp_year']}, the latest "
             "year in the RPP table, before hashing" + (f" (written to {write_to})" if write_to and not errors
                                                         else "; commit the completed manifest")
             for f in filled]
    out = {"hash": manifest_hash(manifest), "path": out_path, "errors": errors, "valid": not errors,
           "notes": notes, "filled": filled}
    if filled:
        out["manifest"] = manifest
    return out


# ---------------------------------------------------------------------------
# Exploratory labelling
# ---------------------------------------------------------------------------


def label_exploratory(result: Any, reason: str = "not in a pre-registered manifest",
                      manifest_hash_value: Optional[str] = None) -> Any:
    """Tag a result (result object with ``provenance`` or a dict) as exploratory.

    Use for any analysis run outside a manifest, including on the same data
    as a manifest run (pass its hash in ``manifest_hash_value``).
    """
    tag = {"analysis_status": STATUS_EXPLORATORY, "exploratory_reason": reason,
           "related_manifest_hash": manifest_hash_value}
    if hasattr(result, "provenance") and isinstance(result.provenance, dict):
        result.provenance.update(tag)
        if hasattr(result, "notes") and isinstance(result.notes, list):
            result.notes.append(f"EXPLORATORY: {reason}")
        return result
    if isinstance(result, dict):
        result.update(tag)
        return result
    raise TypeError("label_exploratory needs a result object with .provenance or a dict")


def analysis_status(result: Any) -> str:
    prov = getattr(result, "provenance", result if isinstance(result, dict) else {})
    return prov.get("analysis_status", STATUS_EXPLORATORY) if isinstance(prov, dict) else STATUS_EXPLORATORY


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _codebook_columns(codebook: pd.DataFrame, concept_id: str) -> Optional[Tuple[str, str, str]]:
    """(estimate slug, moe slug, estimate code) for a concept in a run_batch codebook."""
    rows = codebook[codebook["Concept_ID"].astype(str) == concept_id]
    est = rows[rows["Variable_Slug"].str.endswith(("_est", "_pct")) & ~rows["Variable_Slug"].str.endswith("_moe")]
    if est.empty:
        return None
    e = est.iloc[0]
    slug = str(e["Variable_Slug"])
    moe = f"{slug}_moe" if slug.endswith("_pct") else slug[: -len("_est")] + "_moe"
    return slug, moe, str(e["Variable_Code"])


def _wide_from_panel(panel: pd.DataFrame, columns: Mapping[str, Tuple[str, str, str]]) -> pd.DataFrame:
    """Panel (slugged) -> wide frame keyed by ACS codes (``<base>E``/``<base>M``)."""
    from .stats import parse_variable_code
    out = pd.DataFrame({"GEOID": panel["geoid"].astype(str), "Year": panel["year"].astype("int64")})
    if "name" in panel.columns:
        out["NAME"] = panel["name"]
    for _, (est, moe, code) in columns.items():
        base, suffix = parse_variable_code(code) or (code.rstrip("E"), "E")
        out[f"{base}{suffix}"] = pd.to_numeric(panel[est], errors="coerce").to_numpy()
        out[f"{base}{'PM' if suffix == 'PE' else 'M'}"] = pd.to_numeric(panel[moe], errors="coerce").to_numpy()
    return out


CONFIRMATORY_REPLICATE_POLICY = "strict"
# Manifest methods that use replicates; each runs with replicate_policy="strict".
# (Spatial, multilevel, DiD and PUMS analyses are not manifest methods; if added
# they must join this set so confirmatory runs refuse incomplete replicates.)
REPLICATE_METHODS = ("fit_replicate", "correlation_matrix")


def _one_sided(p: float, estimate: float, direction: str) -> float:
    if direction == "two-sided" or p is None or not np.isfinite(p):
        return p
    agrees = (estimate > 0) if direction == "greater" else (estimate < 0)
    return p / 2 if agrees else 1 - p / 2


def _run_analysis(a: Mapping[str, Any], frame: pd.DataFrame, level: float, replicates: Any,
                  correction: str) -> Any:
    from . import correlation, models, multitest, sae, trends
    a = _normalize_analysis(a)
    method, args = a["method"], dict(a.get("args") or {})
    for k in ("error_covariates", "variables", "variable", "geoid", "variable_1", "variable_2"):
        if k in a:
            args.setdefault(k, a[k])
    if method in REPLICATE_METHODS:
        # Confirmatory runs refuse incomplete replicates (PLAN_ROUND_11 Q2/D7);
        # the failure is recorded as a "replicates_incomplete" deviation.
        args["replicate_policy"] = CONFIRMATORY_REPLICATE_POLICY
    if method == "fit_replicate":
        return models.fit_replicate(a["formula"], frame, replicates=replicates, confidence_level=level, **args)
    if method in ("regression_calibration", "simex"):
        return getattr(models, method)(a["formula"], frame, confidence_level=level, **args)
    if method in ("fay_herriot", "fh_regression"):
        y = a["formula"].split("~")[0].strip()
        args.setdefault("se_col", f"{y}_se")
        return getattr(sae, method)(a["formula"], frame, confidence_level=level, **args)
    if method == "correlation_matrix":
        return correlation.correlation_matrix(frame, args.pop("variables"), confidence_level=level,
                                              replicates=replicates, correction=correction, **args)
    if method == "linear_trend_test":
        v, g = args.pop("variable"), str(args.pop("geoid"))
        sub = frame[frame["GEOID"] == g].sort_values("Year")
        return trends.linear_trend_test(sub["Year"].tolist(), sub[v].tolist(), ses=sub[f"{v}_se"].tolist(),
                                        confidence_level=level, **args)
    if method == "batch_z_test":
        v1, v2 = args.pop("variable_1"), args.pop("variable_2")
        df = frame[["GEOID"]].copy()
        for v, tag in ((v1, "1"), (v2, "2")):
            df[f"e{tag}"] = frame[v].to_numpy(float)
            df[f"m{tag}"] = frame[f"{v}_se"].to_numpy(float) * 1.645
        out = multitest.batch_z_test(df, "e1", "m1", "e2", "m2", confidence_level=level,
                                     correction=correction, **args)
        return out
    raise ValueError(f"unsupported method {method}")


def _statistic(h: Mapping[str, Any], res: Any) -> Dict[str, Any]:
    stat = h["statistic"]
    if stat == "coefficient":
        c = res.coefficients.set_index("term")
        if h["term"] not in c.index:
            raise KeyError(f"term {h['term']!r} not in model (terms: {list(c.index)})")
        row = c.loc[h["term"]]
        from .models import interval_se_column
        se_col = interval_se_column(res)  # the SE behind p_value and the interval
        return {"estimate": float(row["estimate"]), "se": float(row[se_col]), "se_basis": se_col,
                "p_two_sided": float(row["p_value"]),
                "ci_lower": float(row["ci_lower"]), "ci_upper": float(row["ci_upper"])}
    if stat == "correlation":
        a, b = h["variables"]
        return {"estimate": float(res.matrix.loc[a, b]), "se": None, "p_two_sided": float(res.p_values.loc[a, b]),
                "ci_lower": float(res.ci_lower.loc[a, b]), "ci_upper": float(res.ci_upper.loc[a, b])}
    if stat == "trend_slope":
        return {"estimate": res["slope"], "se": res["se_slope"], "p_two_sided": res["p_value"],
                "ci_lower": res["ci_lower"], "ci_upper": res["ci_upper"]}
    if stat == "z_difference":
        row = res[res["GEOID"] == str(h["geoid"])]
        if row.empty:
            raise KeyError(f"GEOID {h['geoid']} not in the test frame")
        r = row.iloc[0]
        diff = float(r.get("difference", r["e1"] - r["e2"]))
        return {"estimate": diff, "se": None, "p_two_sided": float(r["p_value"]), "ci_lower": None, "ci_upper": None}
    raise ValueError(stat)


def _tag(result: Any, h: str, analysis_id: str, status: str = STATUS_CONFIRMATORY) -> Any:
    tag = {"analysis_status": status, "manifest_hash": h, "manifest_analysis_id": analysis_id}
    if hasattr(result, "provenance") and isinstance(result.provenance, dict):
        result.provenance.update(tag)
        return result
    if isinstance(result, pd.DataFrame):
        return {**tag, "rows": result}
    if isinstance(result, dict):
        return {**result, **tag}
    return result


def run_manifest(
    manifest: Union[str, Path, Mapping[str, Any]], output_dir: Union[str, Path], client: Any = None,
    replicates: Any = None, use_cache: bool = True, replicates_synthetic: Optional[bool] = None,
) -> Dict[str, Any]:
    """Execute a manifest exactly: plan -> extraction -> analysis frame ->
    derivations on replicates -> exclusions -> adjustments -> analyses -> hypothesis tests with the pre-registered
    correction -> results bundle (tables, figures, methods.md).

    ``replicates``: optional pre-fetched replicate long frame (from
    :func:`census.analysis.attach_replicates`) used by ``fit_replicate`` and
    ``correlation_matrix``; without it those report model-based SEs and a
    deviation is recorded when the analysis sets ``"replicates": "required"``.
    Replicate analyses run with ``replicate_policy="strict"`` (confirmatory):
    failed refits, rows held fixed, or stochastic variables without replicates
    stop the analysis and record a ``replicates_incomplete`` deviation.
    Supplied replicates are recorded as caller-supplied (not fetched from
    Census VRE files by this function) with the SHA-256 of their canonical form
    (:func:`replicates_sha256`); ``replicates_synthetic`` (default: the data
    source's synthetic flag, or ``replicates.attrs["synthetic"]``) marks them
    synthetic in the methods text.
    ``analysis_status`` is ``"confirmatory"`` only without blocking deviations,
    else ``"confirmatory_incomplete"`` (see the module docstring).
    Never raises on data problems; returns ``success``, ``deviations``,
    ``hypotheses`` and the bundle manifest. The manifest hash is written into
    every output (results JSON, tables, figure sidecars, methods.md, bundle.json).
    """
    from .analysis import analysis_frame
    from .multitest import adjust_pvalues
    from .pipeline import ResearchOptions, plan_extraction, run_batch
    from .report_tables import data_citation

    m = load_manifest(manifest)
    errors = validate_manifest(m, resolve=True, client=client)
    h = manifest_hash(m)
    out = Path(output_dir)
    if errors:
        return {"success": False, "manifest_hash": h, "errors": errors}
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                       encoding="utf-8", newline="\n")
    deviations: List[Dict[str, Any]] = []
    source, mismatch = resolve_data_source(m, client, use_cache)
    if mismatch:
        deviations.append({"kind": "data_source_mismatch", "detail": mismatch})
    level = float(m.get("confidence_level", 0.90))
    corr = m.get("correction") or {}
    corr_method = corr.get("method", "holm")
    releases = [int(r) for r in m["releases"]]
    geo = m["geography"]
    entities = manifest_entities(geo, years=releases)
    opts = ResearchOptions(use_cache=use_cache, include_codebook=True,
                           concordance_mode=m.get("concordance_mode", "fail"))

    # 1-2. plan and extraction, one table at a time
    columns: Dict[str, Tuple[str, str, str]] = {}
    panels: List[pd.DataFrame] = []
    extraction: Dict[str, Any] = {}
    name_of = {str(v["ref"]): str(v["name"]) for v in m["variables"]}
    for table, refs in tables_and_refs(m).items():
        plan = plan_extraction(table, entities, releases, "panel", opts, variables=refs, client=client)
        plan_pub = {k: v for k, v in plan.items() if not k.startswith("_")}
        extraction[table] = {"plan": plan_pub}
        if geo.get("states") is not None:  # one request set per state, stacked into one panel
            extraction[table]["states"] = manifest_states(geo)
        if not plan.get("success"):
            deviations.append({"kind": "blocked_variable", "table": table, "variables": refs,
                               "detail": plan.get("message") or plan.get("error_code")})
            continue
        res = run_batch(table, entities, releases, str(out / "extraction" / table), "panel", opts,
                        client=client, variables=refs)
        extraction[table]["run"] = {k: res.get(k) for k in ("success", "failed_calls", "successful_calls",
                                                             "metadata_path", "panel_path", "advisories")}
        if not res.get("success"):
            deviations.append({"kind": "extraction_failed", "table": table, "variables": refs,
                               "detail": res.get("message") or res.get("error_code")})
            continue
        if res.get("failed_calls"):
            deviations.append({"kind": "failed_calls", "table": table, "detail": res["failed_calls"]})
        if not res.get("panel_path") or not res.get("codebook_path"):
            deviations.append({"kind": "extraction_failed", "table": table, "variables": refs,
                               "detail": "no data returned (every call failed)"})
            continue
        codebook = pd.read_csv(res["codebook_path"], dtype=str)
        cid_of = {s["input"]: s["concept_id"] for s in plan.get("selection", [])}
        tcols = {}
        for ref in refs:
            cols = _codebook_columns(codebook, cid_of.get(ref, ref))
            if cols is None:
                deviations.append({"kind": "variable_missing_from_extraction", "table": table, "variable": ref})
            else:
                tcols[name_of[ref]] = cols
        panel = pd.read_csv(res["panel_path"], dtype={"geoid": str}) if str(res["panel_path"]).endswith(".csv") \
            else pd.read_parquet(res["panel_path"])
        panels.append(_wide_from_panel(panel, tcols))
        columns.update(tcols)

    # 3. analysis frame (optional aggregation first)
    frame = None
    if panels:
        wide = panels[0]
        for p in panels[1:]:
            wide = wide.merge(p.drop(columns=[c for c in ("NAME",) if c in p.columns]), on=["GEOID", "Year"],
                              how="outer")
        refs_by_name = {}
        for name, (_, _, code) in columns.items():
            refs_by_name[name] = code
        if m.get("aggregation"):
            from .aggregate import aggregate_geographies
            try:
                parts = []
                for year, sub in wide.groupby("Year"):
                    agg = aggregate_geographies(sub.drop(columns=["Year"]), m["aggregation"]["mapping"],
                                                estimate_cols=list(refs_by_name.values()))
                    agg["Year"] = year
                    parts.append(agg)
                wide = pd.concat(parts, ignore_index=True)
            except Exception as exc:
                deviations.append({"kind": "aggregation_failed", "detail": str(exc)})
                wide = None
        if wide is not None:
            try:
                frame = analysis_frame(wide, variables=refs_by_name, derive=m.get("derivations") or None)
            except Exception as exc:
                deviations.append({"kind": "analysis_frame_failed", "detail": str(exc)})

    # 3b. derivations on supplied replicates
    work_reps = replicates
    derived_on_replicates: Dict[str, Dict[str, int]] = {}
    if frame is not None and replicates is not None and m.get("derivations"):
        try:
            frame, work_reps, derived_on_replicates = _derive_replicates(frame, replicates, m)
        except Exception as exc:  # noqa: BLE001 - data problems are deviations; run_manifest never raises on them
            # replicate SEs of the derivations are unavailable: blocking, as when a strict replicate
            # analysis meets them; the analyses run on the published SEs and record their own outcome
            deviations.append({"kind": "replicates_incomplete", "detail": "derivations on replicates failed: "
                               f"{type(exc).__name__}: {exc}"})

    # 4. exclusions (before adjustments: a pre-registered exclusion can remove a unit without an
    # RPP area; the CV screen gives the same rows either way, since the RPP factor leaves CVs unchanged)
    exclusion_log: List[Dict[str, Any]] = []
    if frame is not None:
        from .models import reliability_screen
        for ex in m.get("exclusions") or []:
            if ex["rule"] == "geoid_exclude":
                drop = frame["GEOID"].isin([str(g) for g in ex.get("geoids", [])])
                frame = frame[~drop].reset_index(drop=True)
                exclusion_log.append({"rule": "geoid_exclude", "rows_excluded": int(drop.sum())})
            else:
                thr = float(ex.get("threshold", 30.0))
                vars_ = [v for v in ex.get("variables") or [] if f"{v}_cv" in frame.columns]
                flagged = reliability_screen(frame, vars_, threshold=thr)
                entry = {"rule": "cv_screen", "threshold": thr, "variables": vars_,
                         "action": ex.get("action", "flag"), "rows_flagged": int(flagged["GEOID"].nunique())
                         if len(flagged) else 0}
                if entry["action"] == "exclude" and len(flagged):
                    keys = [k for k in ("GEOID", "Year") if k in flagged.columns]
                    bad = frame[keys].merge(flagged[keys].drop_duplicates(), how="left", indicator=True)
                    keep = (bad["_merge"] == "left_only").to_numpy()
                    entry["rows_excluded"] = int((~keep).sum())
                    frame = frame[keep].reset_index(drop=True)
                exclusion_log.append(entry)

    # 4b. pre-registered adjustments (a unit still without an RPP area blocks: adjustment_failed)
    adjustment_log: List[Dict[str, Any]] = []
    if frame is not None and m.get("adjustments"):
        frame, work_reps, adjustment_log, failure = _apply_adjustments(frame, work_reps, m, releases[0])
        if failure:
            deviations.append({"kind": "adjustment_failed", "detail": failure})
            frame = None
    if frame is not None:  # replicate-SE coverage of the units analysed
        counts = {name: _replicate_rows(frame, name) for name in derived_on_replicates}
        derived_on_replicates = {k: v for k, v in counts.items() if v["rows_replicate"]}

    # 5. analyses
    available = set(columns) | {str(d["name"]) for d in m.get("derivations") or []}
    results: Dict[str, Any] = {}
    for a in m["analyses"]:
        a = _normalize_analysis(a)
        aid = a["id"]
        needed = set(_formula_names(a.get("formula", "")))
        for k in ("variables", "error_covariates"):
            needed |= set(a.get(k) or [])
        needed |= {a[k] for k in ("variable", "variable_1", "variable_2") if a.get(k)}
        if frame is None or not needed <= available:
            deviations.append({"kind": "analysis_not_run", "analysis": aid,
                               "detail": f"unavailable variables: {sorted(needed - available)}"
                               if frame is not None else "no analysis frame"})
            continue
        if a.get("replicates") == "required" and replicates is None and a["method"] in REPLICATE_METHODS:
            deviations.append({"kind": "replicates_unavailable", "analysis": aid,
                               "detail": "manifest requires replicate SEs; none supplied, analysis not run"})
            continue
        try:
            results[aid] = _run_analysis(a, frame, level, work_reps, corr_method)
        except Exception as exc:
            kind = "replicates_incomplete" if isinstance(exc, IncompleteReplicatesError) else "analysis_failed"
            deviations.append({"kind": kind, "analysis": aid, "detail": f"{type(exc).__name__}: {exc}"})

    # 6. hypotheses with the pre-registered correction
    rows = []
    for hyp in m["hypotheses"]:
        row = {"id": hyp["id"], "analysis": hyp["analysis"], "statistic": hyp["statistic"],
               "direction": hyp.get("direction", "two-sided"), "family": hyp.get("family", "primary"),
               "statement": hyp.get("statement", "")}
        res = results.get(hyp["analysis"])
        if res is None:
            row.update(status="not_tested", estimate=None, p_value=None)
            deviations.append({"kind": "hypothesis_not_tested", "hypothesis": hyp["id"],
                               "detail": f"analysis {hyp['analysis']} did not run"})
        else:
            try:
                s = _statistic(hyp, res)
                row.update(s, status="tested",
                           p_value=_one_sided(s["p_two_sided"], s["estimate"], row["direction"]))
            except Exception as exc:
                row.update(status="not_tested", estimate=None, p_value=None)
                deviations.append({"kind": "hypothesis_not_tested", "hypothesis": hyp["id"], "detail": str(exc)})
        rows.append(row)
    hyp_df = pd.DataFrame(rows)
    hyp_df["p_adjusted"] = np.nan
    groups = [hyp_df.index] if corr.get("family", "all") == "all" else \
        [g.index for _, g in hyp_df.groupby("family")]
    for idx in groups:
        p = pd.to_numeric(hyp_df.loc[idx, "p_value"], errors="coerce")
        hyp_df.loc[idx, "p_adjusted"] = np.asarray(adjust_pvalues(p.to_numpy(float), corr_method))
    hyp_df["significant"] = hyp_df["p_adjusted"] < (1 - level)
    hyp_df["manifest_hash"] = h

    # 7. bundle
    complete = not any(d["kind"] in BLOCKING_DEVIATIONS for d in deviations)
    status = STATUS_CONFIRMATORY if complete else STATUS_CONFIRMATORY_INCOMPLETE
    rep_info = None
    if replicates is not None:
        if replicates_synthetic is None:
            attrs = getattr(replicates, "attrs", {}) or {}
            replicates_synthetic = bool(attrs.get("synthetic", source["synthetic"]))
        digest, reason = replicates_digest(replicates)
        rep_info = {"source": "caller_supplied", "synthetic": bool(replicates_synthetic),
                    "rows": int(len(replicates)), "sha256": digest}
        if digest is None:
            rep_info["sha256_unavailable"] = reason
        if derived_on_replicates:
            rep_info["derived_on_replicates"] = list(derived_on_replicates)
            rep_info["derived_on_replicates_rows"] = derived_on_replicates
        scaled = [v for a in adjustment_log for v in a["variables"]]
        if scaled:
            rep_info["scaled_by_adjustments"] = scaled
    tagged = {aid: _tag(r, h, aid, status) for aid, r in results.items()}
    run_info = {"manifest_hash": h, "analysis_status": status, "deviations": deviations,
                "exclusions": exclusion_log, "extraction": extraction, "n_units": None if frame is None
                else int(len(frame)), "title": m.get("title"), "data_source": source, "replicates": rep_info}
    if m.get("adjustments"):
        run_info["adjustments"] = adjustment_log
    if not complete:
        run_info["status_note"] = INCOMPLETE_STATUS_TEXT
    tables_used = sorted(tables_and_refs(m))
    citation = data_citation(releases, tables_used, synthetic=source["synthetic"],
                             description=source.get("description"))
    bundle = _write_bundle(out / "bundle", m, h, tagged, hyp_df, run_info, citation)
    out_res = {"success": complete, "analysis_status": status, "data_source": source, "replicates": rep_info,
               "manifest_hash": h, "deviations": deviations, "hypotheses": hyp_df, "results": tagged,
               "frame": frame, "bundle": bundle, "output_dir": str(out), "errors": []}
    if m.get("adjustments"):
        out_res["adjustments"] = adjustment_log
    return out_res


def _replicate_rows(frame: pd.DataFrame, name: str) -> Dict[str, int]:
    """{"rows_replicate": rows whose ``name`` SE is a replicate SE, "rows": all rows}."""
    col = frame.get(f"{name}_method")
    k = 0 if col is None else int((col.astype(str) == "replicate").sum())
    return {"rows_replicate": k, "rows": int(len(frame))}


def _derive_replicates(frame: pd.DataFrame, replicates: Any,
                       m: Mapping[str, Any]) -> Tuple[pd.DataFrame, Any, Dict[str, Dict[str, int]]]:
    """Evaluate manifest derivations missing from the supplied replicate frame on the
    replicates of their inputs (:func:`census.analysis.attach_replicates`, the same
    evaluation as build_analysis_frame). Inputs are found by manifest variable name or
    by ACS code (``B15002_015``). The derived variables' SEs become exact replicate SEs;
    other variables keep theirs. Derivations whose inputs lack replicates are left
    out (a replicate analysis using them then stops as replicates_incomplete).
    Returns (frame, replicates, {derivation: _replicate_rows counts}) listing only
    derivations that received a replicate SE for at least one row. Raises on
    malformed replicates (run_manifest records that as a deviation)."""
    from .analysis import _default_name, attach_replicates
    reps = replicates if isinstance(replicates, pd.DataFrame) else pd.DataFrame(replicates)
    if "variable" not in reps.columns:
        return frame, replicates, {}
    have = {_default_name(v) for v in reps["variable"].astype(str)}

    def refs_of(d: Mapping[str, Any]) -> List[str]:
        return [str(r) for part in ("num", "den")
                for r in (d[part] if isinstance(d.get(part), list) else [d[part]] if d.get(part) is not None else [])]

    var_map: Dict[str, str] = {}  # manifest name -> replicate variable
    for v in m.get("variables") or []:
        name, mm = str(v["name"]), YEAR_CODE_RE.match(str(v.get("ref", "")))
        if name in have:
            var_map[name] = name
        elif mm and _default_name(mm.group(2)) in have:  # B19013_001E, DP04_0046PE as normalized
            var_map[name] = _default_name(mm.group(2))
    todo: List[Dict[str, Any]] = []
    for d in m.get("derivations") or []:
        name, refs = str(d["name"]), refs_of(d)
        if name in have:
            var_map[name] = name
        elif refs and all(r in var_map or r in {str(t["name"]) for t in todo} for r in refs):
            todo.append(dict(d))
    if not todo:
        return frame, replicates, {}
    new_frame, long = attach_replicates(frame, reps, variables=var_map, derive=todo, replace_se=True)
    counts = {str(d["name"]): _replicate_rows(new_frame, str(d["name"])) for d in todo}
    derived = [name for name, c in counts.items() if c["rows_replicate"]]
    if not derived:
        return frame, replicates, {}
    out = frame.copy()
    for name in derived:  # only the derived variables take replicate SEs
        for suffix in ("_se", "_cv", "_method"):
            if f"{name}{suffix}" in new_frame.columns:
                out[f"{name}{suffix}"] = new_frame[f"{name}{suffix}"].to_numpy()
    # derived rows, and rows of inputs found by ACS code renamed to their manifest name
    added = long[long["variable"].isin(derived + [n for n, c in var_map.items() if c != n])]
    combined = pd.concat([reps, added], ignore_index=True)
    combined.attrs = dict(getattr(reps, "attrs", {}) or {})
    return out, combined, {name: counts[name] for name in derived}


def _apply_adjustments(frame: pd.DataFrame, replicates: Any, m: Mapping[str, Any],
                       release: int) -> Tuple[Optional[pd.DataFrame], Any, List[Dict[str, Any]], Optional[str]]:
    """Run the manifest's ``adjustments`` in order with the place_adjust_frame core.
    Returns (frame, replicates, log, failure message or None)."""
    from . import rpp
    from .pipeline import normalize_geo_level
    from .workflows_microdata import place_adjust
    level = normalize_geo_level(m["geography"]["level"])
    log: List[Dict[str, Any]] = []
    for i, adj in enumerate(m.get("adjustments") or []):
        variables = [str(v) for v in adj["variables"]]
        areas = adj.get("areas", default_rpp_areas(level))
        rpp_year = int(adj["rpp_year"])  # required by validation, so the hash fixes it
        try:
            res = place_adjust(frame, variables, release, areas=areas, replicates=replicates, rpp_year=rpp_year)
        except Exception as exc:  # noqa: BLE001 - recorded as a blocking deviation, never substituted
            return None, replicates, log, f"adjustments[{i}] ({adj.get('type')}): {type(exc).__name__}: {exc}"
        frame = res["frame"]
        if replicates is not None:
            replicates = res["replicates"]
        notes = list(res["notes"])
        info = rpp.price_parity_info()
        table: Dict[str, Any] = {"source": "custom" if res["custom_table"] else "bundled"}
        if res["custom_table"]:
            table.update(res["custom_table"])
        else:
            table["sha256"] = (info.get("csv_sha256") or {}).get("rpp.csv")
        log.append({"type": adj["type"], "variables": variables, "areas": areas, "dollar_year": int(release),
                    "rpp_year": res["rpp_year"], "rpp_year_source": "manifest",
                    "rpp_year_matches_release": res["rpp_year"] == int(release),
                    "rpp_areas": res["rpp_areas"], "boundary_source": res["boundary_source"],
                    "rpp_table": table, "rows": int(len(frame)), "notes": notes,
                    "advisories": res["advisories"], "applied_by_default": False})
    return frame, replicates, log, None


def effective_data_source(manifest: Mapping[str, Any], client: Any = None) -> Dict[str, Any]:
    """Declaration-level data source (before a run): the manifest's
    ``data_source`` (default: not synthetic), forced synthetic when ``client``
    is observed synthetic. Runs use :func:`resolve_data_source`."""
    ds = dict(manifest.get("data_source") or {})
    ds["synthetic"] = bool(ds.get("synthetic")) or (client is not None and observed_data_source(client)[0])
    return ds


def observed_data_source(client: Any = None, use_cache: bool = True) -> Tuple[bool, Dict[str, Any]]:
    """(synthetic, marker) for the client that serves the data: synthetic when
    the client has a true ``synthetic`` attribute or its cache (for
    ``client=None``: the default cache, when ``use_cache``) carries a
    ``QueryCache.mark_synthetic`` marker. ``marker`` holds its description and
    generator ({} when none)."""
    if client is None:
        from .cache import get_default_cache
        cache = get_default_cache() if use_cache else None
    else:
        cache = getattr(client, "cache", None)
    marker = None
    if cache is not None and hasattr(cache, "synthetic_marker"):
        try:
            marker = cache.synthetic_marker()
        except Exception:  # noqa: BLE001 - unreadable marker: treat as none
            marker = None
    synthetic = marker is not None or (client is not None and getattr(client, "synthetic", False) is True)
    return synthetic, dict(marker or {})


def data_source_record(client: Any = None, use_cache: bool = True) -> Dict[str, Any]:
    """Observed data source as recorded in extraction metadata and results bundles:
    {"kind": "synthetic" | "census_api", "synthetic": bool} plus the synthetic
    marker's description and generator when set (:func:`observed_data_source`)."""
    synthetic, marker = observed_data_source(client, use_cache)
    record: Dict[str, Any] = {"kind": "synthetic" if synthetic else "census_api", "synthetic": synthetic}
    record.update({k: marker[k] for k in ("description", "generator") if marker.get(k)})
    return record


def resolve_data_source(manifest: Mapping[str, Any], client: Any = None,
                        use_cache: bool = True) -> Tuple[Dict[str, Any], Optional[str]]:
    """Data-source label for a run: the observed source wins over the declaration.

    Returns (source, mismatch). ``source["synthetic"]`` is the observed value;
    ``declared_synthetic`` the manifest's (None when undeclared). On a
    mismatch the declared description/generator are dropped (they describe
    data that were not used) and ``mismatch`` explains it.
    """
    ds = manifest.get("data_source")
    ds = dict(ds) if isinstance(ds, Mapping) else {}
    declared = ds.get("synthetic") if isinstance(ds.get("synthetic"), bool) else None
    observed, marker = observed_data_source(client, use_cache)
    mismatch = None
    if declared is not None and declared != observed:
        mismatch = (f"manifest declares data_source.synthetic={str(declared).lower()} but the data came from a "
                    + ("synthetic source (synthetic client or marked cache)" if observed
                       else "non-synthetic source (Census Data API client or unmarked cache)")
                    + "; labelled by the observed source")
        ds = {}
    source: Dict[str, Any] = {k: v for k, v in ds.items() if k != "synthetic"}
    if observed:
        for k in ("description", "generator"):
            if not source.get(k) and marker.get(k):
                source[k] = marker[k]
    source.update(synthetic=observed, declared_synthetic=declared)
    return source, mismatch


def _canonical_column(col: pd.Series) -> List[str]:
    """String form of one column: numbers (int or float, not numeric strings) as repr(float)
    (shortest round-trip text, identical across pandas/numpy versions; 3 and 3.0 alike),
    NaN/None as "", everything else str(); identical across pandas/numpy versions."""
    out: List[str] = []
    numeric = pd.api.types.is_numeric_dtype(col.dtype) and not pd.api.types.is_bool_dtype(col.dtype)
    for v in col.tolist():
        if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
            out.append("")
        elif numeric or (isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_))):
            f = float(v)
            out.append("" if math.isnan(f) else repr(f))
        else:
            out.append(str(v))
    return out


def replicates_digest(replicates: Any) -> Tuple[Optional[str], Optional[str]]:
    """(sha256, None) of the canonical CSV of a replicate frame, or (None, reason). Never raises.

    Canonical form (JSON lines): column labels as strings (a collision after str() is an
    error), columns sorted by label, every value as a string (numbers as
    ``repr(float(x))`` so 3 and 3.0 hash alike; missing as ""), rows sorted
    by those strings (stable), no index, UTF-8, LF line endings.
    """
    try:
        df = replicates if isinstance(replicates, pd.DataFrame) else pd.DataFrame(replicates)
        labels = [str(c) for c in df.columns]
        dups = sorted({x for x in labels if labels.count(x) > 1})
        if dups:
            return None, f"column labels collide as strings: {dups}"
        cols = {lab: _canonical_column(df.iloc[:, i]) for i, lab in enumerate(labels)}
        order = sorted(labels)
        rows = sorted(zip(*(cols[c] for c in order))) if order else []
        lines = [json.dumps(order, ensure_ascii=False)] + [json.dumps(list(r), ensure_ascii=False) for r in rows]
        return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest(), None
    except Exception as exc:  # noqa: BLE001 - provenance hashing must never stop a run
        return None, f"{type(exc).__name__}: {exc}"


def read_replicates(path: Union[str, Path]) -> pd.DataFrame:
    """Read a replicate long frame (CSV or Parquet) with GEOID as text and floats
    parsed exactly (round-trip) on every pandas version, so the same file gives the
    same values and :func:`replicates_sha256` everywhere."""
    path = str(path)
    if path.lower().endswith(".parquet"):
        return pd.read_parquet(path)
    kw: Dict[str, Any] = {"dtype": {"GEOID": str}}
    if int(pd.__version__.split(".")[0]) < 3:  # pandas 3 parses round-trip by default (option deprecated)
        kw["float_precision"] = "round_trip"
    return pd.read_csv(path, **kw)


def replicates_sha256(replicates: Any) -> Optional[str]:
    """SHA-256 of a replicate frame's canonical form (:func:`replicates_digest`); None if unavailable."""
    return replicates_digest(replicates)[0]


def _write_bundle(folder: Path, m, h, results, hyp_df, run_info, citation) -> Dict[str, Any]:
    from . import figures as figs
    from .report_tables import ReportTable, correlation_table, regression_table
    from .results import CorrelationResult, ModelResult, SmallAreaResult
    note = f"Pre-registered analysis; manifest SHA-256 {h}."
    fig_source = f"{citation} {note}"
    tables, figures_ = {}, {}
    model_results = {aid: r for aid, r in results.items() if isinstance(r, ModelResult)}
    if model_results:
        t = regression_table(model_results, correction=(m.get("correction") or {}).get("method", "holm"))
        t.notes.append(note)
        t.data = t.data.assign(manifest_hash=h)
        tables["regression"] = t
        figures_["coefficients"] = (figs.coefficient_plot, {"results": model_results, "manifest_hash": h,
                                                            "data_source": fig_source})
    for aid, r in results.items():
        if isinstance(r, CorrelationResult):
            t = correlation_table(r)
            t.notes.append(note)
            t.data = t.data.assign(manifest_hash=h)
            tables[f"correlation_{aid}"] = t
            figures_[f"correlation_{aid}"] = (figs.correlation_heatmap, {"result": r, "manifest_hash": h,
                                                                         "data_source": fig_source})
        if isinstance(r, SmallAreaResult):
            figures_[f"shrinkage_{aid}"] = (figs.shrinkage_plot, {"result": r, "manifest_hash": h,
                                                                  "data_source": fig_source})
    cols = ["id", "statistic", "direction", "estimate", "p_value", "p_adjusted", "significant", "status"]
    show = hyp_df.reindex(columns=cols)
    tables["hypotheses"] = ReportTable(
        title="Pre-registered hypotheses", display=show.astype(object).where(show.notna(), "").astype(str),
        data=hyp_df, notes=[note, f"p-values adjusted by {(m.get('correction') or {}).get('method', 'holm')} "
                                  f"(family: {(m.get('correction') or {}).get('family', 'all')})."])
    payload = dict(results)
    payload["hypotheses"] = {"manifest_hash": h, "rows": hyp_df}
    payload["run"] = run_info
    from .report_tables import results_bundle
    provenance = {"manifest_hash": h, "analysis_status": run_info["analysis_status"],
                  "deviations": len(run_info["deviations"]), "data_source": run_info.get("data_source"),
                  "replicates": run_info.get("replicates")}
    if "adjustments" in run_info:  # RPP provenance: years, areas, table hash
        provenance["adjustments"] = [{k: a[k] for k in ("type", "variables", "areas", "dollar_year", "rpp_year",
                                                        "rpp_year_source", "rpp_areas", "boundary_source",
                                                        "rpp_table")} for a in run_info["adjustments"]]
    return results_bundle(folder, tables=tables, figures=figures_, methods=methods_section(m, run_info),
                          results=payload, provenance=provenance, citation=citation)


# ---------------------------------------------------------------------------
# Methods text
# ---------------------------------------------------------------------------

_METHOD_TEXT = {
    "fit_replicate": ("Coefficients were estimated with {model} regression (statsmodels) on the full-sample "
                      "estimates; sampling standard errors came from refitting on each of {rep_source}, Var = 4/80 sum (b_r - b)^2, combined with the model standard error as "
                      "se_total = sqrt(se_model^2 + se_sampling^2). Reported standard errors, intervals and p-values use "
                      "the {target_se} ({target} target: {estimand}), with normal-theory intervals. Without "
                      "replicates only model standard errors are reported.", ["vre", "design", "statsmodels"]),
    "regression_calibration": ("Covariates measured with known ACS sampling error ({cov}) were corrected by "
                               "regression calibration with heteroscedastic known error variances; standard "
                               "errors (se_total, used for normal-theory intervals and p-values) by row bootstrap "
                               "re-estimating the calibration. Coefficients are {estimand}.", ["carroll"]),
    "simex": ("Covariates measured with known ACS sampling error ({cov}) were corrected by "
              "simulation-extrapolation (SIMEX, quadratic extrapolation) with the measurement-error jackknife "
              "variance (se_total, used for normal-theory intervals and p-values). Coefficients are "
              "{estimand}.", ["simex", "carroll"]),
    "fh_regression": ("The outcome's known sampling variance was modelled with an area-level Fay-Herriot model "
                      "(GLS coefficients, REML variance component).", ["fh"]),
    "fay_herriot": ("Small-area estimates are Fay-Herriot EBLUPs (REML) with Prasad-Rao/Datta-Lahiri MSE.",
                    ["fh"]),
    "correlation_matrix": ("Correlations with Fisher-z intervals{rep}.", ["fisher", "vre"]),
    "linear_trend_test": ("Linear trends across non-overlapping 5-year periods were tested by GLS with known "
                          "sampling variances.", ["testing"]),
    "batch_z_test": ("Differences were tested with the Census two-sample z test, SE = MOE/1.645.", ["testing"]),
}


def methods_section(manifest: Mapping[str, Any], results: Optional[Mapping[str, Any]] = None) -> str:
    """Markdown methods text matching what the manifest specifies (and, with
    ``results`` = the run info of :func:`run_manifest`, what actually ran)."""
    from .report_tables import data_citation
    from .sae import REFERENCES as FH_REFS
    m = manifest
    h = manifest_hash(m)
    run = results or {}
    source = run.get("data_source") or effective_data_source(m)
    rep_info = run.get("replicates")
    if rep_info:
        kind = "synthetic caller-supplied" if rep_info.get("synthetic") else "caller-supplied"
        rep_source = (f"the {kind} replicate estimates (not fetched from Census variance replicate files by "
                      + (f"the toolkit; canonical SHA-256 `{rep_info['sha256']}`)" if rep_info.get("sha256")
                         else f"the toolkit; SHA-256 unavailable: {rep_info.get('sha256_unavailable')})"))
        rep_short = f" and replicate-based standard errors from {kind} replicate estimates"
    elif results is not None:
        rep_source = "replicate estimates when supplied (none were supplied to this run)"
        rep_short = " and replicate-based standard errors"
    else:
        rep_source = "the 80 ACS replicate estimates"
        rep_short = " and replicate-based standard errors"
    level = float(m.get("confidence_level", 0.90))
    corr = m.get("correction") or {}
    cites: List[str] = ["acs"]
    lines = [f"## Methods", "", f"*Pre-registered analysis \"{m.get('title', '')}\"; manifest SHA-256 `{h}`, "
             f"created {m.get('created')} by {', '.join(m.get('authors', []))}.*", ""]
    if results is not None:
        status = results.get("analysis_status", STATUS_CONFIRMATORY)
        lines += [f"**Analysis status: {status}**"
                  + (f": {INCOMPLETE_STATUS_TEXT}." if status == STATUS_CONFIRMATORY_INCOMPLETE else "."), ""]
    lines += ["### Data", ""]
    tables = sorted(tables_and_refs(m))
    geo = m.get("geography", {})
    cite = data_citation(m.get("releases", []), tables, synthetic=source["synthetic"],
                         description=source.get("description"))
    lines.append(f"Data are {cite} Geography: {geo.get('level')}"
                 + (f" (state {geo.get('state')})" if geo.get("state") else "")
                 + (f" (states {', '.join(map(str, geo['states']))}, extracted per state and stacked; no "
                    "automatic state effects, a state term enters only where a formula names one)"
                    if geo.get("states") else "")
                 + (f", codes {', '.join(map(str, geo['codes']))}" if geo.get("codes") else "") + ".")
    if source["synthetic"] and source.get("generator"):
        lines.append(f"Synthetic data generator: {source['generator']}.")
    lines.append("Variables (YEAR:CODE references resolve to that vintage's concept; concept IDs follow the "
                 "toolkit's cross-vintage concordance, with blocking concordance issues stopping extraction "
                 "rather than being substituted):")
    lines += [f"- `{v['name']}`: {v['ref']}" + (f" (table {v['table']})" if v.get("table") else "")
              for v in m.get("variables", [])]
    if len(m.get("releases", [])) > 1:
        lines.append("Multiple releases were linked through the concordance; overlapping 5-year periods "
                     "share sample years and are not independent.")
    if m.get("aggregation"):
        lines.append(f"Geographies were aggregated into {len(set(m['aggregation']['mapping'].values()))} groups; "
                     "aggregate MOEs use replicate variance where available, else the ACS approximation "
                     "sqrt(sum MOE^2).")
        cites.append("vre")
    lines += ["", "### Measures and uncertainty", "",
              "Standard errors of published estimates are MOE/1.645 (ACS MOEs are 90% margins)."]
    rep_rows = (rep_info or {}).get("derived_on_replicates_rows") or {}
    for d in m.get("derivations") or []:
        c = rep_rows.get(d["name"])
        if c and c["rows_replicate"] >= c["rows"]:
            se_text = f"; SE from the derivation evaluated on each replicate (all {c['rows']} units)"
        elif c:
            se_text = (f"; replicate SE for {c['rows_replicate']} of {c['rows']} units; approximate SE (ACS "
                       "formulas, Handbook ch. 8) elsewhere")
        else:
            se_text = "; MOE by the ACS approximation formulas (Handbook ch. 8)"
        lines.append(f"- `{d['name']}` = {d.get('op', 'auto')} of {d.get('num')}"
                     + (f" over {d['den']}" if d.get("den") is not None else "") + se_text + ".")
    for ex in m.get("exclusions") or []:
        if ex["rule"] == "cv_screen":
            lines.append(f"Observations with CV > {ex.get('threshold', 30)}% on {', '.join(ex.get('variables', []))} "
                         f"were {'excluded' if ex.get('action') == 'exclude' else 'kept and flagged'}.")
        else:
            lines.append(f"{len(ex.get('geoids', []))} pre-specified GEOIDs were excluded.")
    run_adj = {i: a for i, a in enumerate(run.get("adjustments") or [])}
    for i, adj in enumerate(m.get("adjustments") or []):
        done = run_adj.get(i)
        year = (done or {}).get("rpp_year") or adj.get("rpp_year")
        areas = (done or {}).get("areas") or adj.get("areas") or "default"
        lines.append(f"- Price adjustment{' (after the exclusions)' if m.get('exclusions') else ''}: "
                     f"{', '.join(adj.get('variables', []))} divided by the BEA Regional "
                     f"Price Parity ({year}, U.S. = 100) / 100 of each "
                     "unit's area"
                     + (" (the county's metropolitan statistical area, else the state's nonmetropolitan portion; "
                        "CBSA membership from Census boundaries of the OMB 23-01 delineation)"
                        if areas == "county_metro" else " (its state)" if areas == "state" else "")
                     + "; estimates, SEs and any supplied replicates are scaled by the same factor and CVs are "
                     "unchanged. "
                     "RPPs are treated as fixed: their own estimation error is not in any SE or interval.")
        if done:
            if not done.get("rpp_year_matches_release"):
                lines.append(f"  Note: the {done['rpp_year']} RPP was applied to dollars of {done['dollar_year']} "
                             "(the release end year).")
            lines.append(f"  {len(done.get('rpp_areas') or [])} RPP area(s) were used; RPP table "
                         + (f"custom {done['rpp_table'].get('path')} (SHA-256 {done['rpp_table'].get('sha256')}, "
                            "not verified)" if done["rpp_table"].get("source") == "custom"
                            else f"bundled (SHA-256 {done['rpp_table'].get('sha256')})") + ".")
    lines.append("Rows with missing or undefined estimates were dropped analysis by analysis (complete cases).")
    lines += ["", "### Analyses", ""]
    for a in m.get("analyses", []):
        a = _normalize_analysis(a)
        text, refs = _METHOD_TEXT[a["method"]]
        args = a.get("args") or {}
        tgt = args.get("target", "population")
        # the same interpretation contract as the fitted result (census.results.interval_description)
        probe = ModelResult(model_type="", formula=str(a.get("formula") or ""), n_obs=0,
                            coefficients=pd.DataFrame(columns=["term"]), method=a["method"], uncertainty_sources=[],
                            confidence_level=level,
                            diagnostics={"interval_target": tgt} if a["method"] == "fit_replicate" else {})
        info = interval_description(probe)
        text = text.format(model=args.get("model", "ols").upper(), target=tgt, estimand=info["estimand"],
                           target_se={"se_model": "model SE (se_model)", "se_sampling": "replicate sampling SE "
                                      "(se_sampling)", "se_total": "combined SE (se_total)"}[info["basis_column"]],
                           cov=", ".join(a.get("error_covariates") or args.get("error_covariates") or []),
                           rep=rep_short if (a.get("replicates") or rep_info) else "", rep_source=rep_source)
        target = a.get("formula") or a.get("variables") or a.get("variable")
        lines.append(f"- **{a['id']}** (`{target}`): {text}")
        cites += refs
    lines += ["", "### Hypotheses and multiple comparisons", ""]
    for hyp in m.get("hypotheses", []):
        lines.append(f"- **{hyp['id']}** ({hyp.get('direction', 'two-sided')}, {hyp['statistic']}"
                     + (f" of `{hyp['term']}`" if hyp.get("term") else "") + f"): {hyp.get('statement', '')}")
    cm = str(corr.get("method", "holm")).lower()
    lines.append(f"Intervals are {level:.0%}. p-values were adjusted with the {cm} procedure across "
                 f"{'all hypotheses' if corr.get('family', 'all') == 'all' else 'each hypothesis family'}; "
                 f"one-sided p-values are half the two-sided value in the hypothesized direction.")
    cites.append({"benjamini-hochberg": "bh", "fdr_bh": "bh", "fdr_by": "by"}.get(cm, cm))
    if results is not None:
        devs = results.get("deviations", [])
        lines += ["", "### Deviations from the manifest", ""]
        lines += [f"- {d['kind']}: " + ", ".join(f"{k}={v}" for k, v in d.items() if k != "kind")
                  for d in devs] or ["None."]
        for e in results.get("exclusions", []):
            lines.append(f"- Exclusion {e['rule']}: " + ", ".join(f"{k}={v}" for k, v in e.items() if k != "rule"))
    lines += ["", "### References", ""]
    seen = []
    for c in cites:
        if c in seen:
            continue
        seen.append(c)
        if c == "fh":
            lines += [f"- {r}" for r in FH_REFS]
        elif c in REFERENCES:
            lines.append(f"- {REFERENCES[c]}")
    return "\n".join(lines) + "\n"
