# SPDX-License-Identifier: AGPL-3.0-or-later
"""File- and dict-level workflows shared by the MCP server and the CLI.

Each function takes plain arguments (paths, lists, dicts), never raises for
user errors, and returns a JSON-serializable dict with ``success`` and, on
failure, ``error_code`` (census.errors). The MCP server registers these
functions directly as tools, so their docstrings are the tool descriptions.
"""

import functools
import math
import os
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from census import errors
from census.aggregate import NonAdditiveError, aggregate_then_derive
from census.aggregate import aggregate_geographies as _aggregate_geographies
from census.api import CensusAPIClient, infer_dataset
from census.cache import get_default_cache, import_frozen, read_frozen
from census.derived import difference as derive_difference
from census.derived import percent_change as derive_percent_change
from census.derived import product as derive_product
from census.derived import proportion as derive_proportion
from census.derived import ratio as derive_ratio
from census.derived import sum_estimates
from census.geo import get_resource_path
from census.geo_crosswalk import harmonize_panel
from census.medians import distribution_brackets, median_for_group, median_for_rows
from census.medians import median_from_distribution as _median_from_distribution
from census.metadata import latest_vintage, table_variables
from census.multitest import batch_z_test
from census.pipeline import replay_extraction as _replay_extraction
from census.replicate import VREDownloadError, fetch_vre, vre_availability
from census.review import draft_rule_override as _draft_rule
from census.review import record_rule_review as _record_rule
from census.variance import DerivedEstimate

ROOT = get_resource_path("")


def _default_year(dataset: str = "acs5") -> int:
    return latest_vintage(dataset) or 2024


def _client(use_cache: bool = True) -> CensusAPIClient:
    return CensusAPIClient(cache=get_default_cache() if use_cache else None)


_FIX_NEXT_STEP = "fix the arguments as described in `error` and call again, or get_recipe for the standard steps"


def _guided(summary: Callable[[Dict[str, Any]], str], next_step: Union[str, Callable[[Dict[str, Any]], str]]):
    """Give a workflow result native ``summary`` (one sentence) and ``next_step``.

    Errors (``success`` False with ``error``/``error_code``) get the error message
    as summary and a fix-and-retry next step; other unsuccessful results (e.g.
    failed validation checks) keep the workflow's own summary and next step.
    The wrapped function keeps its name, docstring and signature (MCP schema).
    """

    def decorate(fn: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            out = fn(*args, **kwargs)
            if not isinstance(out, dict):
                return out
            if out.get("success") is False and (out.get("error") or out.get("error_code") or out.get("message")):
                text, step = str(out.get("error") or out.get("message") or "The call failed."), _FIX_NEXT_STEP
            else:
                try:
                    text = summary(out)
                except (KeyError, TypeError, ValueError, IndexError):
                    text = "Done."
                step = next_step(out) if callable(next_step) else next_step
            rest = {k: v for k, v in out.items() if k not in ("summary", "next_step")}
            return {"summary": out["summary"] if isinstance(out.get("summary"), str) else text,
                    "next_step": out.get("next_step") or step, **rest}

        return wrapper

    return decorate


def _num(value: Any, spec: str = ".4g") -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "undefined"
    return format(v, spec) if math.isfinite(v) else "undefined"


def _derived_summary(out: Dict[str, Any]) -> str:
    if "estimate" in out:
        est, moe = _num(out.get("estimate")), _num(out.get("moe"))
        if est == "undefined":
            return f"{out.get('method', 'derived')} estimate undefined (see notes)."
        moe_txt = f"90% MOE {moe}" if moe != "undefined" else "MOE undefined (see notes)"
        return f"{out.get('method', 'derived')} estimate {est} with {moe_txt}."
    return f"{len(out.get('results') or [])} row(s) computed."


_DERIVE_OPS = ("proportion", "ratio", "product", "difference", "percent_change", "sum")


def _jsonable(value: Any) -> Any:
    """Convert numpy/pandas scalars, arrays, frames and NaN to JSON-friendly values."""
    if isinstance(value, pd.DataFrame):
        if not isinstance(value.index, pd.RangeIndex):
            value = value.reset_index()
        return [_jsonable(r) for r in value.to_dict(orient="records")]
    if isinstance(value, pd.Series):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _records(df: pd.DataFrame, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = df if limit is None else df.head(limit)
    return [_jsonable(r) for r in rows.to_dict(orient="records")]


def _load_frame(path: str) -> pd.DataFrame:
    """Read a CSV or Parquet output and restore API code column names.

    Panels written with slugged names are mapped back through the
    <table>_codebook.csv next to them (Variable_Slug -> Variable_Code).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No such file: {path}")
    if path.lower().endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path, dtype={"GEOID": str, "geoid": str}, low_memory=False)
    if any(str(c).endswith(("E", "M")) and "_" in str(c) and str(c)[:1].isupper() for c in df.columns):
        return df
    folder = os.path.dirname(os.path.abspath(path))
    # Panels are named <TABLE>_<geo>_panel_<span>; use only that table's codebook.
    # Label slugs (e.g. total_est) repeat across tables, so applying every
    # codebook in the folder could rename columns with another table's codes.
    table = os.path.basename(path).split("_", 1)[0].upper()
    books = [n for n in os.listdir(folder) if n.endswith("_codebook.csv")]
    own = f"{table}_codebook.csv"
    chosen = own if own in books else (books[0] if len(books) == 1 else None)
    if chosen:
        book = pd.read_csv(os.path.join(folder, chosen), dtype=str)
        if {"Variable_Code", "Variable_Slug"} <= set(book.columns):
            mapping = dict(zip(book["Variable_Slug"], book["Variable_Code"]))
            df = df.rename(columns={c: mapping.get(c, c) for c in df.columns})
    rename = {c: c.upper() for c in df.columns if str(c).lower() in ("geoid", "name", "year")}
    rename = {c: ("Year" if v == "YEAR" else v) for c, v in rename.items()}
    return df.rename(columns=rename)


def _write_frame(df: pd.DataFrame, output_path: Optional[str], default_name: str) -> str:
    path = output_path or os.path.join(ROOT, "downloads", default_name)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.lower().endswith(".parquet"):
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False, encoding="utf-8")
    return path


def _derived_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, DerivedEstimate):
        return {"success": True, **_jsonable(result.to_dict())}
    return {"success": True, "results": _records(result)}


@_guided(_derived_summary, "calculate_significance to compare this estimate with another")
def derive_estimate(
    operation: str,
    estimates: List[float],
    moes: List[float],
    as_percent: bool = False,
    year_current: Optional[int] = None,
    year_prior: Optional[int] = None,
) -> Dict[str, Any]:
    """Derive an estimate and its 90% MOE with the Census approximation formulas
    (ACS General Handbook 2020, ch. 8).

    Args:
        operation: 'proportion' (numerator is a SUBSET of the denominator, e.g.
            people in poverty / people for whom poverty is determined), 'ratio'
            (not a subset, e.g. males / females), 'product', 'difference'
            (first minus second), 'percent_change' (first = current, second =
            prior), or 'sum' (any number of components).
        estimates: [numerator, denominator] (or [x, y]); for 'sum', all components.
        moes: Published 90% MOEs aligned with estimates.
        as_percent: Return proportion/ratio on a 0-100 scale.
        year_current, year_prior: For percent_change between ACS releases (adds the
            overlapping-period note).

    Returns estimate, moe, se, cv_percent, method, and notes. method records the
    formula used, e.g. 'proportion_ratio_fallback' when the proportion radicand
    was negative. These formulas ignore covariance; when Census publishes
    replicate estimates for the table (check_replicate_availability), prefer
    aggregate_geographies(method='replicate') or the replicate functions for
    estimates that feed a hypothesis test.
    """
    op = operation.lower().strip()
    if op not in _DERIVE_OPS:
        return errors.error_result(errors.INVALID_INPUT, f"operation must be one of {_DERIVE_OPS}.")
    if len(estimates) != len(moes) or not estimates:
        return errors.error_result(errors.INVALID_INPUT, "estimates and moes must be non-empty and aligned.")
    if op == "sum":
        return _derived_result(sum_estimates(estimates, moes))
    if len(estimates) != 2:
        return errors.error_result(errors.INVALID_INPUT, f"'{op}' takes exactly two estimates.")
    (x, y), (mx, my) = estimates, moes
    if op == "proportion":
        res = derive_proportion(x, mx, y, my, as_percent=as_percent)
    elif op == "ratio":
        res = derive_ratio(x, mx, y, my, as_percent=as_percent)
    elif op == "product":
        res = derive_product(x, mx, y, my)
    elif op == "difference":
        res = derive_difference(x, mx, y, my)
    else:
        res = derive_percent_change(x, mx, y, my, year_current=year_current, year_prior=year_prior)
    return _derived_result(res)


@_guided(
    lambda o: ("Replicate tables are available." if o.get("available") else
               f"Replicate tables are not available: {o.get('reason', '')}".rstrip(": ") + "."),
    lambda o: ("aggregate_geographies or median_from_distribution with use_replicates=True for exact MOEs"
               if o.get("available") else
               "use the approximate formulas (derive_estimate; aggregate_geographies method='approximate')"),
)
def check_replicate_availability(table_code: str, year: int, geo_level: str) -> Dict[str, Any]:
    """Whether the Census Bureau publishes Variance Replicate Estimate tables
    (80 replicates, exact variances) for a detailed table, ACS 5-year release, and
    geography level. Available for releases 2014 onward and a listed subset of
    detailed (B/C) tables; never for medians or profile/subject tables.

    Returns available, reason, summary_level, per_state_files, and the download
    url or url_template.
    """
    try:
        return {"success": True, **_jsonable(vre_availability(table_code.upper(), year, geo_level))}
    except ValueError as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))


@_guided(
    lambda o: f"Aggregated into {o['groups']} group(s); written to {o['output_path']}.",
    "derive_estimate for rates, or batch_significance on output_path to compare groups",
)
def aggregate_geographies(
    input_path: str,
    groups: Dict[str, str],
    variables: Optional[List[str]] = None,
    derive: Optional[List[Dict[str, Any]]] = None,
    year: Optional[int] = None,
    method: str = "auto",
    use_replicates: bool = False,
    table_code: Optional[str] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Combine geographies (e.g. tracts into neighborhoods) into custom areas, with
    MOEs, then optionally derive rates on the combined counts.

    Args:
        input_path: CSV or Parquet from batch_download (wide CSV or panel; slugged
            panel columns are mapped back to codes via the codebook next to it, so
            download with include_codebook=True or slug_style='code').
        groups: {GEOID: group_id}. Every listed GEOID must be in the file.
        variables: Estimate columns to sum (e.g. ['B17001_001E', 'B17001_002E']);
            default all additive estimate columns.
        derive: Optional derive_columns spec applied AFTER aggregation, e.g.
            [{"name": "poverty_rate", "op": "proportion", "num": "B17001_002E",
            "den": "B17001_001E", "as_percent": true}]. Never average member rates.
        year: For panels, the release year to aggregate (required if several).
        method: 'auto' (replicate when available, else approximate), 'replicate',
            or 'approximate'.
        use_replicates: Download Census Variance Replicate Estimates for the table
            (requires table_code, year; tract files are per state) for exact MOEs.
        table_code: Detailed table code for replicate lookups.
        output_path: Where to write the result (default downloads/).

    Medians, means, percentages, and ratios are refused (NonAdditiveError): sum the
    underlying counts or distribution, then use derive / median_from_distribution.
    Output columns per variable: <base>E, <base>M, _SE, _CV, _METHOD, _NOTES.
    """
    try:
        df = _load_frame(input_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    if "Year" in df.columns:
        years = sorted(pd.unique(df["Year"]))
        if year is None and len(years) > 1:
            return errors.error_result(errors.INVALID_INPUT, f"File has several years {years}; pass year=.")
        if year is not None:
            df = df[df["Year"] == year]
    replicate_frame = None
    if use_replicates:
        if not (table_code and year):
            return errors.error_result(errors.INVALID_INPUT, "use_replicates needs table_code and year.")
        try:
            replicate_frame = _replicates_for(table_code, year, df, list(groups))
        except (VREDownloadError, ValueError) as exc:
            return errors.error_result(errors.API_HTTP_ERROR, f"Replicate download failed: {exc}")
    try:
        if derive:
            out = aggregate_then_derive(
                df, groups, derive, estimate_cols=variables, method=method, replicate_frame=replicate_frame,
                metadata=_metadata_for_frame(df, table_code, year),
            )
        else:
            out = _aggregate_geographies(
                df, groups, estimate_cols=variables, method=method, replicate_frame=replicate_frame,
                metadata=_metadata_for_frame(df, table_code, year),
            )
    except NonAdditiveError as exc:
        return errors.error_result(errors.INVALID_VARIABLES, str(exc))
    except (KeyError, ValueError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    path = _write_frame(out, output_path, "aggregated_geographies.csv")
    return {"success": True, "output_path": path, "groups": int(len(out)), "preview": _records(out, 10)}


def _metadata_for_frame(df: pd.DataFrame, table_code: Optional[str], year: Optional[int]) -> Dict[str, Any]:
    """Variable labels for additivity checks, from the offline archive."""
    tables = {table_code.upper()} if table_code else set()
    for col in df.columns:
        head = str(col).split("_", 1)[0]
        if "_" in str(col) and head[:1].isalpha() and head[1:2].isdigit():
            tables.add(head.upper())
    meta: Dict[str, Any] = {}
    for table in tables:
        dataset = infer_dataset(table)
        variables_, _source = table_variables(table, dataset, year or _default_year(dataset), None)
        meta.update(variables_ or {})
    return meta


def _replicates_for(table_code: str, year: int, df: pd.DataFrame, geoids: List[str]) -> pd.DataFrame:
    geoids = [str(g) for g in geoids]
    lengths = {len(g) for g in geoids}
    level = {2: "state", 5: "county", 7: "place", 11: "tract"}.get(lengths.pop() if len(lengths) == 1 else 0)
    if level is None:
        raise ValueError("Replicate lookup needs GEOIDs of one geography level (state, county, place, tract).")
    if level == "tract":
        frames = [fetch_vre(table_code.upper(), year, "tract", state_code=s) for s in sorted({g[:2] for g in geoids})]
        return pd.concat(frames, ignore_index=True)
    return fetch_vre(table_code.upper(), year, level)


@_guided(_derived_summary, "calculate_significance to compare this median with another")
def median_from_distribution(
    table_code: str,
    year: int,
    input_path: Optional[str] = None,
    geoids: Optional[List[str]] = None,
    counts: Optional[List[float]] = None,
    per_row: bool = False,
    use_replicates: bool = False,
    design_factor: Optional[float] = None,
) -> Dict[str, Any]:
    """Median and MOE from a distribution table (e.g. B19001 household income
    brackets, B25063 gross rent, B25075 home value), by linear interpolation as
    in the ACS Subject Definitions.

    Use it for areas Census does not publish (combined tracts): bracket counts
    are summed across geoids, then interpolated. Medians are never averaged.

    Args:
        table_code: Distribution table; bracket bounds come from that release's
            labels in the metadata archive.
        year: ACS 5-year release.
        input_path: CSV/Parquet from batch_download containing the table's
            estimate (and MOE) columns. Or pass counts instead.
        geoids: GEOIDs to combine (default: all rows in the file).
        counts: Bracket counts in table order, instead of input_path.
        per_row: Return one median per row instead of one combined median.
        use_replicates: Exact MOE from Census Variance Replicate Estimates.
        design_factor: Use the Census PUMS design-factor method for the MOE
            approximation. Only when you have a justified factor; the default
            approximation fits the variance function to the published bracket MOEs
            (a documented adaptation, not a Census-published formula).

    Medians in the open top bracket return the bracket's lower bound with a '+'
    note and no MOE (Census's own convention). Agreement with published medians:
    gross rent reproduces Census to the dollar; household income from B19001 is
    usually within 1% (Census interpolates a finer internal distribution).
    """
    table = table_code.upper()
    try:
        brackets = distribution_brackets(table, year)
    except (ValueError, KeyError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    if counts is not None:
        if len(counts) != len(brackets):
            return errors.error_result(
                errors.INVALID_INPUT, f"{table} {year} has {len(brackets)} brackets; got {len(counts)} counts.",
                brackets=[b[3] for b in brackets],
            )
        res = _median_from_distribution([b[1] for b in brackets], [b[2] for b in brackets], counts)
        return _derived_result(res)
    if not input_path:
        return errors.error_result(errors.INVALID_INPUT, "Pass input_path or counts.")
    try:
        df = _load_frame(input_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    if "Year" in df.columns:
        df = df[df["Year"] == year]
    try:
        if per_row:
            out = median_for_rows(df, table, year, brackets=brackets, design_factor=design_factor)
            ids = df[[c for c in ("GEOID", "NAME") if c in df.columns]].reset_index(drop=True)
            return {"success": True, "results": _records(pd.concat([ids, out.reset_index(drop=True)], axis=1))}
        replicate_frame = None
        if use_replicates:
            ids = geoids or list(df["GEOID"].astype(str))
            replicate_frame = _replicates_for(table, year, df, ids)
        res = median_for_group(
            df, table, year, geoids=geoids, brackets=brackets, replicate_frame=replicate_frame,
            design_factor=design_factor,
        )
    except (VREDownloadError,) as exc:
        return errors.error_result(errors.API_HTTP_ERROR, f"Replicate download failed: {exc}")
    except (KeyError, ValueError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    return _derived_result(res)


@_guided(
    lambda o: (f"{o['significant_adjusted']} of {o['family_size']} geographies differ significantly after "
               f"{o['correction']} correction ({o['significant_raw']} before correction)."),
    "report significant_adjusted (not significant_raw) and name the correction",
)
def batch_significance(
    input_path: str,
    est1: str,
    moe1: str,
    est2: str,
    moe2: str,
    year1: Optional[int] = None,
    year2: Optional[int] = None,
    confidence_level: float = 0.90,
    correction: str = "holm",
    output_path: Optional[str] = None,
    product: str = "5-year",
) -> Dict[str, Any]:
    """Test many geographies at once for a difference between two estimates, with
    a multiple-comparison correction.

    Args:
        input_path: Wide CSV/Parquet with one row per geography and the four
            columns named below (e.g. a file with 2018 and 2023 values side by side).
        est1, moe1, est2, moe2: Column names.
        year1, year2: ACS release end years, to apply the overlapping-period
            adjustment (5-year only). Prefer non-overlapping periods (5+ years apart).
        confidence_level: 0.90 matches ACS MOEs.
        correction: 'holm' (default; family-wise error), 'bonferroni', 'bh'
            (false discovery rate, for exploratory screening), or 'by'.
        product: '5-year' (default) or '1-year'. 1-year releases never share
            sample, so no overlap adjustment is applied.

    Testing 3,000 tracts at 90% yields about 300 false positives with no
    correction. Report significant_adjusted, not significant_raw.
    """
    try:
        df = _load_frame(input_path)
        out = batch_z_test(df, est1, moe1, est2, moe2, year1, year2, confidence_level, correction, product)
    except (FileNotFoundError, OSError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    except (KeyError, ValueError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    path = _write_frame(out, output_path, "batch_significance.csv")
    sig = out["significant_adjusted"].fillna(False).astype(bool)
    return {
        "success": True,
        "output_path": path,
        "family_size": int(out["family_size"].iloc[0]) if len(out) else 0,
        "significant_raw": int(out["significant_raw"].fillna(False).astype(bool).sum()),
        "significant_adjusted": int(sig.sum()),
        "correction": correction,
        "preview": _records(out[sig], 20),
    }


@_guided(
    lambda o: f"Harmonized {o['rows']} row(s) to one tract vintage; written to {o['output_path']}.",
    "build_analysis_frame or batch_significance on output_path",
)
def apportion_tracts(
    input_path: str,
    target_vintage: int = 2020,
    weight: str = "auto",
    weights_file: Optional[str] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Harmonize a tract panel across the 2010/2020 tract boundary change.

    ACS releases through 2019 use 2010 tracts; 2020 onward use 2020 tracts.
    Rows from releases on the other tract vintage are apportioned:
    estimate = sum(w * X), MOE = sqrt(sum((w * MOE)^2)). weight="auto" (default)
    uses decennial block weights (housing units for household/housing tables,
    population otherwise), falling back to land area with a note when block data
    cannot be fetched; "population", "housing_units", "land_area", or an
    NHGIS-style weights_file (source_geoid, target_geoid, weight) are also accepted. Connecticut's 2022 planning-region GEOIDs are
    handled. Medians, means, and percentages are refused: apportion counts, then
    re-derive. Prefer batch_download(harmonize_tracts_to=...) for new extractions.
    """
    try:
        df = _load_frame(input_path)
        out, record = harmonize_panel(
            df, target_vintage=target_vintage, weight=weight, weights_file=weights_file,
            metadata=_metadata_for_frame(df, None, None), return_record=True,
        )
    except (FileNotFoundError, OSError) as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    except ValueError as exc:
        return errors.error_result(errors.INVALID_VARIABLES, str(exc))
    path = _write_frame(out, output_path, f"tract_panel_harmonized_{target_vintage}.csv")
    return {"success": True, "output_path": path, "rows": int(len(out)), "harmonization": _jsonable(record)}


@_guided(
    lambda o: ("Replay verified: outputs match the original extraction." if o.get("verified")
               else "Replay finished; outputs differ from the original or were not verified (see explanation)."),
    lambda o: ("cite the replay in your methods" if o.get("verified")
               else "report provenance_differences and the changed cells; they are not errors"),
)
def replay_extraction(
    metadata_path: str, verify: bool = True, output_dir: Optional[str] = None, refresh: bool = False
) -> Dict[str, Any]:
    """Re-run an extraction from its <table>_extraction_metadata.json (table,
    dataset, years, exact entities, variables, research options, custom cpi_table)
    and verify it.

    Outputs go to output_dir (default <original dir>/replays/<timestamp>; the
    original directory is refused). With verify, every output file's SHA-256 is
    compared with the original and changed CSV/Parquet/Excel files are compared
    cell by cell (files[path].diff.changes_sample: GEOID, year, column, old, new).
    Census occasionally revises published values: differences return
    success=True, verified=False with an explanation and provenance_differences
    (package version, metadata archive SHAs, overrides). Report them; they are not
    errors. refresh=True bypasses the query cache to query the live API (needed to
    detect revisions); otherwise cached responses reproduce the original offline.
    Metadata written before replay support returns INVALID_INPUT with
    missing_fields; re-run the original extraction instead.
    """
    return _jsonable(_replay_extraction(metadata_path, output_dir=output_dir, verify=verify, refresh=refresh))


def draft_rule_override(
    table_code: str,
    from_year: int,
    to_year: int,
    pattern: str,
    replacement: str,
    drafted_by: str,
    rationale: str,
    source_url: str = "",
    dataset: str = "",
) -> Dict[str, Any]:
    """Draft a table-scoped concordance rule: one reviewed regex rewrite that
    resolves many relabeled lines at a table redesign (e.g. a header part such as
    'population for whom poverty status is determined' inserted into every label).

    The rule rewrites canonical labels (lowercase, '!!'-separated parts, as shown
    in check_variable_concordance issues) of from_year lines with
    re.sub(pattern, replacement, label); a rewritten line whose parts equal a
    to_year line's parts is merged with it. to_year must be the next published
    release. Returns a preview (labels_rewritten, new_matches, still_unmatched),
    guardrail_errors, a national continuity preview, and a draft row with empty
    reviewed_by. Evidence is saved to crosswalks/evidence/ only when guardrails
    pass. Rejected: rules that change any number in a label, map two earlier codes
    to one later line, match nothing, or cite a non-census.gov source. Hand the
    draft row to a DIFFERENT reviewer (record_rule_review).
    """
    dataset = dataset or infer_dataset(table_code)
    return _jsonable(
        _draft_rule(table_code.upper(), dataset, from_year, to_year, pattern, replacement,
                    drafted_by, rationale, source_url, client=_client())
    )


def record_rule_review(row: Dict[str, str], reviewed_by: str, dataset: str = "") -> Dict[str, Any]:
    """Record an independently reviewed rule row in
    crosswalks/<dataset>/<table>_rules.csv.

    reviewed_by must be your own identity and differ from drafted_by. Before
    calling, read every rewrite and match in crosswalks/evidence/<evidence_id>.json
    and the cited Census source. The row is re-validated (regenerated evidence,
    guardrails, conflicts with other rules); on failure nothing is written and
    error_code is INVALID_INPUT. Merged concepts are still continuity-checked at
    extraction; a rule grants no exemption.
    """
    dataset = dataset or infer_dataset(row.get("table", ""))
    return _jsonable(_record_rule(row, reviewed_by, dataset, client=_client()))


@_guided(
    lambda o: ("All validation checks passed." if o.get("success") else
               "Validation failed: " + ", ".join(o["checks_summary"]["failed"] + o["checks_summary"]["errors"]) + "."),
    lambda o: ("cite docs/audits/validation_report.md" if o.get("success")
               else "inspect failures of each failed check in results"),
)
def run_validation(
    checks: Optional[str] = None,
    live: bool = False,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Recompute values the Census Bureau publishes and compare them with this
    toolkit's calculations (the project's end-to-end correctness test).

    Checks: F1 pipeline cells vs raw API cells; F2 Data Profile percentages and
    MOEs from detailed tables; F3 replicate MOEs vs published MOEs; F4 exact
    replicate proportions vs Data Profile MOEs; F5 medians from distribution
    tables vs published medians; F6 Comparison Profile inflation adjustment
    (R-CPI-U-RS); F7 Comparison Profile significance flags; F8 county sums vs
    state estimates; F9 concordance continuity; F10 table-based Summary File vs
    API.

    Args:
        checks: Comma-separated subset such as 'F2,F4' (default: all).
        live: Query api.census.gov and www2.census.gov (slower, needs
            CENSUS_API_KEY) instead of the frozen fixtures shipped with the tests.
        output_dir: Also write validation_report.md/.json here. By default no
            files are written; docs/audits/validation_report.md holds the latest
            committed live report.

    Returns success (all checks passed), summary (one line), checks_summary
    (passed/failed/errors), and per-check results with
    n_compared, n_failed, max errors, tolerance, Census sources, and failures.
    """
    from census import validation

    try:
        parsed = validation.parse_checks(checks)
    except ValueError as exc:
        return errors.error_result(errors.INVALID_INPUT, str(exc))
    payload = validation.run_validation(
        checks=parsed, live=live, output_dir=output_dir, write_reports=bool(output_dir)
    )
    # The per-check tally is ``checks_summary``; ``summary`` is the one-line text.
    payload["checks_summary"] = payload.pop("summary")
    return _jsonable(payload)


# ---------------------------------------------------------------------------
# Round 9 (G2): slim download for the core MCP profile
# ---------------------------------------------------------------------------

def _parse_geography(geography: str) -> tuple:
    """'county' -> ('county', None); 'county:453,201' -> ('county', ['453', '201'])."""
    level, _, codes = str(geography or "").partition(":")
    code_list = [c.strip() for c in codes.split(",") if c.strip()] or None
    return level.strip().lower(), code_list


@_guided(
    lambda o: (f"Downloaded {o['table_code']} {o.get('years_requested')}: {o['successful_calls']} of "
               f"{o['total_calls_attempted']} call(s) succeeded; panel at {o.get('panel_path')}."),
    "derive_estimate for rates, or build_analysis_frame(input_path=panel_path) for models",
)
def download_table(
    table_code: str,
    geography: str,
    state_code: Optional[str] = None,
    years: Optional[List[int]] = None,
    variables: Optional[List[str]] = None,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Download one ACS 5-year table to a tidy panel (CSV) with codebook, SE/CV
    and reliability columns. Plans first and stops if the plan would block.

    Args:
        table_code: e.g. 'B17001', 'DP03', 'S1701'.
        geography: 'us', 'state', 'county', 'tract', 'place', 'zcta' or 'msa';
            add ':codes' to pick areas, e.g. 'county:453,201' or 'place:35000'.
        state_code: 2-digit state FIPS (required for county, tract, place).
        years: ACS 5-year releases, e.g. [2018, 2023] (default: latest).
        variables: Codes the analysis uses, e.g. ['B17001_001', 'B17001_002']
            (default: the whole table, which blocks more often across years).
        output_dir: Destination folder (default downloads/).

    Tract releases spanning 2019 and 2020 are apportioned to 2020 tracts. For
    any other option use batch_download (full profile).
    """
    from census.pipeline import ResearchOptions, build_entities, plan_extraction, public_plan, run_batch

    try:
        level, codes = _parse_geography(geography)
    except ValueError as exc:
        return errors.error_result(errors.INVALID_GEO_LEVEL, str(exc))
    table = str(table_code).strip().upper()
    year_list = sorted({int(y) for y in years}) if years else [_default_year(infer_dataset(table))]
    options = ResearchOptions(compute_reliability=True, include_codebook=True)
    if level == "tract" and year_list[0] <= 2019 < year_list[-1]:
        options.harmonize_tracts_to = 2020  # releases span the 2010/2020 tract change
    client = _client()
    try:
        entities = build_entities(level, state_code, codes, client=client, years=year_list)
    except ValueError as exc:
        return errors.error_result(errors.INVALID_GEO_LEVEL, str(exc))
    plan = plan_extraction(table, entities, year_list, "panel", options, None, variables, client)
    if not plan.get("success"):
        return _jsonable(plan)
    if plan.get("would_block"):
        info = public_plan(plan)
        return {
            "success": False,
            "error_code": errors.CONCORDANCE_REVIEW_REQUIRED,
            "error": f"Plan would block: {plan.get('block_reason') or 'unresolved concordance issues'}.",
            "would_block": True,
            "issues": _jsonable(info.get("issues", [])[:10]),
            "issue_count": info.get("issue_count"),
            "next_step": "name fewer variables (variables=[...]) or fewer years, or use check_variable_concordance",
        }
    base_dir = output_dir or os.path.join(ROOT, "downloads")
    out = run_batch(table, entities, year_list, base_dir, "panel", options, None, client, variables=variables,
                    plan=plan)
    out = _jsonable(out)
    if isinstance(out, dict) and out.get("success"):
        out["years_requested"] = year_list
    return out


# ---------------------------------------------------------------------------
# Round 14 (D2): frozen API responses into the query cache (CLI `cache import`)
# ---------------------------------------------------------------------------

def import_into_cache(cache: Any, source: Optional[str], allow_default_cache: bool = False,
                      allow_hint: str = "allow_default_cache=true") -> Dict[str, Any]:
    """Shared by the CLI ``cache import`` and the MCP ``cache_import`` tool.

    Every file is validated before anything is written (atomic, as
    :func:`census.cache.import_frozen`); synthetic responses go into the
    default cache (CENSUS_CACHE_PATH unset) only with ``allow_default_cache``.
    ``allow_hint`` names that switch in the refusal for the caller's interface.
    """
    if cache is None:
        return errors.error_result(errors.INVALID_INPUT, "The query cache is disabled "
                                   "(CENSUS_CACHE_DISABLED); nothing to import into.")
    if not source:
        return errors.error_result(errors.INVALID_INPUT, "cache import needs a frozen file or directory")
    try:
        frozen = read_frozen(source)  # every file validated before anything is written
        if (any(f["synthetic"] is not None for f in frozen) and not os.getenv("CENSUS_CACHE_PATH")
                and not allow_default_cache):
            return errors.error_result(
                errors.INVALID_INPUT,
                f"Refusing to import SYNTHETIC responses into the default query cache ({cache.path}): the "
                "marker would label every later result built from that cache synthetic, including real "
                "Census data. Set CENSUS_CACHE_PATH to a separate cache file (e.g. export "
                f"CENSUS_CACHE_PATH=$PWD/synthetic_cache.db), or pass {allow_hint}.",
                next_step="set CENSUS_CACHE_PATH and run cache import again")
        result = import_frozen(cache, frozen)
    except (OSError, ValueError) as exc:
        return errors.error_result(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}")
    return {"success": True, **result, **cache.stats()}


def cache_import(source: str, allow_default_cache: bool = False) -> Dict[str, Any]:
    """Load frozen Census API responses into the local query cache, so later
    extractions and run_manifest read them instead of api.census.gov (the
    CLI's ``python -m census cache import``).

    A frozen file is JSON with ``request`` (build_request arguments: year,
    geo_level, state_code, ...), ``requests`` ({table_code: API rows}) and,
    for generated data, ``synthetic``. Atomic: every file is validated first;
    on any error nothing is written. A synthetic source marks the whole cache
    synthetic, so every later result built from it is labelled synthetic.

    Args:
        source: A frozen response JSON file, or a directory of them.
        allow_default_cache: Allow SYNTHETIC responses into the default cache
            (CENSUS_CACHE_PATH unset). Refused by default: set
            CENSUS_CACHE_PATH to a separate cache file instead.

    Returns success, imported (responses), sources (path, sha256, tables,
    synthetic), synthetic (whether the cache is marked), and the cache's path
    and entries.
    """
    return import_into_cache(get_default_cache(), source, allow_default_cache)
