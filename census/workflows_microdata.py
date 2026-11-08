# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 10 workflows (PUMS microdata, research designs, place price levels,
interactive maps) shared by the MCP server and the CLI. Same conventions as
census.workflows_inference: plain arguments in, JSON-safe dicts out, with
``summary`` and ``next_step``; failures carry ``error_code``.
"""

import os
import re
from typing import Any, Dict, List, Optional

from census import errors
from census.data_integrity import BundledDataIntegrityError, integrity_error_result
from census.workflows import _load_frame, _records, _write_frame
from census.results import interval_description
from census.workflows_inference import _fail, _ok, _out_dir, _read_replicates, interval_next_step, result_summary

_ERRORS = (FileNotFoundError, OSError, KeyError, ValueError, TypeError, RuntimeError, ZeroDivisionError)
_PUMS_NAME = re.compile(r"\b[A-Z][A-Z0-9_]{1,15}\b")


def _pums_columns(*texts: Optional[str]) -> List[str]:
    """PUMS variable names mentioned in var/by/where/formula text (upper-case codes)."""
    names: List[str] = []
    for text in texts:
        for name in _PUMS_NAME.findall(text or ""):
            if name not in names and name not in ("AND", "OR", "NOT", "IN"):
                names.append(name)
    return names


def _memory_warnings(df: Any) -> List[str]:
    """attrs["memory_note"] from census.pums.read_pums_csv (columns=None on a large file), as a list."""
    note = (getattr(df, "attrs", None) or {}).get("memory_note")
    return [f"MEMORY: {note}"] if note else []


def _warning_suffix(warns: List[str]) -> str:
    return f" WARNING: {' '.join(warns)}" if warns else ""


def pums_tabulate(
    state: str,
    year: int,
    statistic: str = "count",
    var: Optional[str] = None,
    by: Optional[str] = None,
    where: Optional[str] = None,
    weight: str = "person",
    product: str = "5-year",
    output_path: Optional[str] = None,
    denominator: Optional[str] = None,
) -> Dict[str, Any]:
    """Custom tabulation from ACS PUMS microdata with replicate-weight standard
    errors (Census successive-difference replication, 80 replicate weights).

    Use it when no published table crosses the variables you need.

    Args:
        state: 2-digit FIPS. year: release end year. product: "5-year" or "1-year".
        statistic: "count", "mean", "proportion", "median", or "ratio".
        var: PUMS variable (e.g. "PINCP"); by: grouping variable (e.g. "PUMA" or "DIS").
        where: pandas query on PUMS codes, e.g. "AGEP >= 25 and AGEP < 65".
        weight: "person" (PWGTP) or "housing" (WGTP).
        denominator: required for statistic="ratio": sum(w*var) / sum(w*denominator),
            e.g. var="WAGP", denominator="PINCP" (records with both defined).
    Estimates from fewer than 30 records or with CV above 30% are flagged, not
    suppressed. Results reproduce Census PUMS verification estimates.
    Example: pums_tabulate("56", 2024, "median", var="PINCP", where="AGEP >= 15")
    """
    from census import pums

    record = "housing" if weight == "housing" else "person"
    try:
        columns = _pums_columns(var, by, where, denominator)
        df = pums.fetch_pums(state, year, product=product, record=record, columns=columns or None)
        out = pums.tabulate(df, statistic, var, by=by, where=where, weight=weight, denominator=denominator)
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    path = _write_frame(out, output_path, "pums_tabulation.csv")
    flags = [c for c in ("flag_small_n", "flag_high_cv", "flag_se_undefined") if c in out.columns]
    flagged = int(out[flags].astype(bool).any(axis=1).sum()) if flags else 0
    warns = _memory_warnings(df)
    return _ok(f"PUMS {statistic} for {len(out)} cell(s); {flagged} flagged (fewer than 30 records, "
               "CV over 30%, or undefined uncertainty)." + _warning_suffix(warns),
               "report estimates with their MOE; treat flagged cells with caution",
               output_path=path, preview=_records(out, 20), warnings=warns)


def pums_regression(
    state: str,
    year: int,
    formula: str,
    weight: str = "person",
    model: str = "ols",
    where: Optional[str] = None,
    product: str = "5-year",
    target: str = "these_areas",
) -> Dict[str, Any]:
    """Survey-weighted regression on PUMS microdata with replicate-weight SEs
    (the design-based inference Census recommends for PUMS).

    Args:
        formula: e.g. "np.log(WAGP) ~ AGEP + C(SCHL)" (PUMS codes).
        model: "ols", "logit", or "poisson". where: pandas query to subset records.
        target: "these_areas" (default; the coefficient in the surveyed population,
            replicate-weight SE) or "population" (model SE).
    Example: pums_regression("56", 2024, "WAGP ~ AGEP", where="WAGP > 0")
    """
    from census import pums

    record = "housing" if weight == "housing" else "person"
    try:
        df = pums.fetch_pums(state, year, product=product, record=record,
                             columns=_pums_columns(formula, where) or None)
        res = pums.pums_regression(formula, df, weight=weight, model=model, where=where, target=target)
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    out = res.to_dict()
    warns = _memory_warnings(df)
    return _ok(result_summary(res) + _warning_suffix(warns),
               "report coefficients with the SE named in uncertainty_sources",
               **{k: v for k, v in out.items() if k not in ("summary", "warnings")}, warnings=warns)


def multilevel_model(
    input_path: str,
    formula: str,
    groups: str,
    re_formula: Optional[str] = None,
    sampling_se: Optional[str] = None,
    replicates_path: Optional[str] = None,
    target: str = "population",
) -> Dict[str, Any]:
    """Multilevel (mixed) model, e.g. tracts within counties, with Kenward-Roger
    small-sample intervals; ACS sampling error enters through a known level-1
    variance (sampling_se) or replicate refits.

    Args:
        groups: grouping column, e.g. "county". re_formula: random slopes, e.g. "~x".
        sampling_se: outcome SE column (known level-1 variance).
    Validated: 90% intervals cover 0.89 in simulation.
    Example: multilevel_model("frame.parquet", "poverty_rate ~ unemployment_rate", "county", sampling_se="poverty_rate_se")
    """
    from census.multilevel import fit_multilevel

    try:
        frame = _load_frame(input_path)
        res = fit_multilevel(formula, frame, groups, re_formula=re_formula, sampling_se=sampling_se,
                             replicates=_read_replicates(replicates_path), target=target)
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    out = res.to_dict()
    return _ok(result_summary(res), interval_next_step(interval_description(res)),
               **{k: v for k, v in out.items() if k != "summary"})


def difference_in_differences(
    input_path: str,
    outcome: str,
    treated: str,
    treatment_period: int,
    period: str = "Year",
    se: Optional[str] = None,
    design: str = "two_group",
    reference: Optional[int] = None,
    replicates_path: Optional[str] = None,
    target: str = "population",
    product: str = "5-year",
    period_years: Optional[int] = None,
) -> Dict[str, Any]:
    """Difference-in-differences on area-level ACS estimates across NON-overlapping
    periods (e.g. 2013, 2018, 2023 releases); overlapping releases are refused.

    Args:
        treated: 0/1 column marking treated areas. treatment_period: first treated release.
        se: outcome SE column (known sampling variance). design: "two_group" or "event".
        reference: pre-treatment release used as the baseline.
        product: "5-year" (default) or "1-year"; sets the period length used for
            the overlap check (5 or 1) unless period_years is given.
    With 3+ pre-periods a parallel-trends test is reported. Validated: coverage 0.90-0.91.
    Example: difference_in_differences("panel.csv", "poverty_rate", "treated", 2023, se="poverty_rate_se")
    """
    from census.api import PRODUCT_PERIOD_YEARS, normalize_product
    from census.did import difference_in_differences as _did

    try:
        years = int(period_years) if period_years is not None else PRODUCT_PERIOD_YEARS[normalize_product(product)]
        frame = _load_frame(input_path)
        res = _did(frame, outcome, treated, treatment_period, period=period, se=se, design=design,
                   reference=reference, replicates=_read_replicates(replicates_path), target=target,
                   period_years=years)
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    out = res.to_dict()
    return _ok(result_summary(res), "report the DiD estimate with its interval and the parallel-trends result",
               **{k: v for k, v in out.items() if k != "summary"})


def adjust_for_place(
    value: float,
    geography: str,
    year: int,
    state: Optional[str] = None,
    base_year: Optional[int] = None,
    product: str = "5-year",
) -> Dict[str, Any]:
    """Convert dollars to national-average purchasing power with BEA Regional
    Price Parities (state, metro area, or nonmetro portion); with base_year also
    convert to that year's dollars with R-CPI-U-RS. Never applied automatically.

    Args:
        geography: state FIPS, CBSA code, or "US". year: the dollars' year.
        product: "5-year" (default) or "1-year": the ACS product the dollars come from.
    RPPs carry their own estimation error, which is not in ACS MOEs.
    Example: adjust_for_place(75000, "06", 2023)
    """
    from census import rpp
    from census.api import PRODUCT_PERIOD_YEARS, normalize_product

    try:
        period_years = PRODUCT_PERIOD_YEARS[normalize_product(product)]
        if base_year is None:
            adjusted = rpp.adjust_for_place(value, geography, year, state=state)
        else:
            adjusted = rpp.adjust_for_place_and_time(value, geography, year, base_year, state=state)
        described = rpp.describe_place_adjustment(geography, year, state=state, base_year=base_year,
                                                  acs_period_years=period_years)
    except BundledDataIntegrityError as exc:
        return integrity_error_result(exc)
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    return _ok(f"{value:,.0f} in {geography} ({year}) = {adjusted:,.0f} at national-average prices"
               + (f" in {base_year} dollars" if base_year else "") + ".",
               "state in results that a regional price parity adjustment was applied",
               value=value, adjusted_value=adjusted, geography=geography, year=year, base_year=base_year,
               adjustment=described)


_RPP_AREAS = ("county_metro", "state")


def _county_rpp_areas(geoids: List[str], year: int) -> Any:
    """GEOID -> CBSA membership (CBSA, CBSA_NAME, LSAD) from Census boundaries: the
    ACS release's county file and the CBSA file of BEA's delineation."""
    from census import rpp
    from census.boundaries import county_cbsa_membership, get_boundaries
    from census.workflows_geo import _fetch_boundaries

    counties = _fetch_boundaries("county", year, sorted({g[:2] for g in geoids}))
    counties = counties[counties["GEOID"].astype(str).isin(set(geoids))]
    missing = sorted(set(geoids) - set(counties["GEOID"].astype(str)))
    if missing:
        raise ValueError(f"no {year} county boundaries for {missing[:5]}")
    cbsas = get_boundaries("cbsa", rpp.RPP_CBSA_BOUNDARY_YEAR)
    return county_cbsa_membership(counties, cbsas).set_index("GEOID"), cbsas.attrs.get("source_url")


def place_adjust(frame: Any, variables: List[str], year: int, areas: str = "county_metro",
                 replicates: Any = None, rpp_year: Optional[int] = None) -> Dict[str, Any]:
    """Core of :func:`place_adjust_frame` and of the manifest ``adjustments`` step
    (census.manifest): the one implementation of the RPP adjustment of a frame.

    ``frame``: analysis frame (DataFrame, GEOID per row; a modified copy is returned).
    ``variables``: dollar variables; ``<v>`` and ``<v>_se`` are divided by RPP/100.
    ``year``: the dollars' year (ACS: release end year; also the county boundary year).
    ``rpp_year``: the RPP year (default ``year``). ``replicates``: optional replicate
    long frame; rows of ``variables`` are scaled by the same factor, and their rows
    whose GEOID is not in the frame are dropped.
    Returns {"frame", "replicates", "rpp_year", "parity", "rpp_areas", "boundary_source",
    "notes", "advisories", "custom_table"}. Raises on bad input or a missing RPP.
    """
    import pandas as pd

    from census import rpp
    from census.analysis import REPLICATE_COLUMNS

    if areas not in _RPP_AREAS:
        raise ValueError(f"areas must be one of {_RPP_AREAS}.")
    if isinstance(variables, str):
        variables = [variables]
    variables = list(variables)
    rpp_year = int(year if rpp_year is None else rpp_year)
    frame = frame.copy()
    missing = [v for v in variables if v not in frame.columns or f"{v}_se" not in frame.columns]
    if missing:
        raise KeyError(f"frame lacks {missing} (or their _se columns); use analysis variable names")
    if "Year" in frame.columns and set(pd.to_numeric(frame["Year"]).astype(int)) - {int(year)}:
        raise ValueError(f"frame has releases other than {int(year)}; adjust one release at a time")
    geoids = [str(g) for g in frame["GEOID"]]
    boundary_source = None
    if areas == "county_metro":
        if any(len(g) != 5 for g in geoids):
            raise ValueError("areas='county_metro' needs county GEOIDs (5 digits); use areas='state'")
        membership, boundary_source = _county_rpp_areas(sorted(set(geoids)), int(year))
        member = {g: membership.loc[g] for g in set(geoids)}
        keys = [rpp.county_rpp_area(g, member[g]["CBSA"] if pd.notna(member[g]["CBSA"]) else None,
                                    member[g]["LSAD"] if pd.notna(member[g]["LSAD"]) else None)
                for g in geoids]
        frame["cbsa"] = [member[g]["CBSA"] if pd.notna(member[g]["CBSA"]) else "" for g in geoids]
        frame["cbsa_type"] = [{"M1": "metropolitan", "M2": "micropolitan"}.get(member[g]["LSAD"], "none")
                              if pd.notna(member[g]["LSAD"]) else "none" for g in geoids]
    else:
        keys = [g[:2] for g in geoids]
    parity = {k: rpp.place_parity(k, rpp_year) for k in sorted(set(keys))}
    frame["rpp_area"] = [parity[k]["geo_fips"] for k in keys]
    frame["rpp_area_name"] = [parity[k]["geo_name"] for k in keys]
    frame["rpp"] = [parity[k]["rpp"] for k in keys]
    factor = 100.0 / frame["rpp"].to_numpy(float)
    for v in variables:
        frame[v] = pd.to_numeric(frame[v], errors="coerce").to_numpy(float) * factor
        frame[f"{v}_se"] = pd.to_numeric(frame[f"{v}_se"], errors="coerce").to_numpy(float) * factor
    reps = None
    if replicates is not None:
        reps = replicates.copy()
        by_geoid = dict(zip(geoids, factor))
        sel = reps["variable"].isin(variables)
        unmatched = sorted(set(reps.loc[sel, "GEOID"].astype(str)) - set(by_geoid))
        f = reps.loc[sel, "GEOID"].astype(str).map(by_geoid).to_numpy(float)
        cols = ["estimate"] + [c for c in REPLICATE_COLUMNS if c in reps.columns]
        # whole-number replicate files read as int64; pandas 3 refuses float values in int columns
        reps[cols] = reps[cols].astype(float)
        reps.loc[sel, cols] = reps.loc[sel, cols].to_numpy(float) * f[:, None]
        if unmatched:
            reps = reps[~(sel & reps["GEOID"].astype(str).isin(unmatched))]
    counts = frame.groupby(["rpp_area", "rpp_area_name"]).agg(rpp=("rpp", "first"), rows=("rpp", "size")).reset_index()
    notes = [f"PLACE ADJUSTMENT: {', '.join(variables)} divided by the BEA Regional Price Parity ({rpp_year}, "
             "U.S.=100) of each row's area, i.e. expressed in national-average purchasing power; _se scaled by "
             "the same factor."]
    if rpp_year == int(year):
        notes.append(f"ACS 5-year dollars are in end-year ({int(year)}) dollars; the {int(year)} RPP is applied to "
                     "the whole period.")
    else:
        notes.append(f"RPP YEAR: the {rpp_year} RPP is applied to ACS dollars of {int(year)} (the release end "
                     f"year); price-level differences between places are assumed unchanged between {rpp_year} "
                     f"and {int(year)}.")
    if areas == "county_metro":
        notes.append("RPP AREA: counties in a metropolitan CBSA use that MSA's RPP; micropolitan and rural "
                     "counties use the state nonmetropolitan-portion RPP (BEA nonmetro portions include "
                     f"micropolitan counties). CBSA membership from {boundary_source} (OMB bulletin 23-01 "
                     "delineation, as in BEA's RPPs).")
    # provenance as in rpp.describe_place_adjustment: advisories and an unverified custom RPP table
    advisories = sorted({a for p in parity.values() for a in p["advisories"]})
    custom = next((p["custom_table"] for p in parity.values() if p.get("custom_table")), None)
    notes += advisories
    if custom:
        notes.append(rpp.custom_table_note(custom))
    notes.append(rpp.RPP_ESTIMATION_ERROR_NOTE)
    return {"frame": frame, "replicates": reps, "rpp_year": rpp_year, "parity": parity,
            "rpp_areas": _records(counts), "boundary_source": boundary_source, "notes": notes,
            "advisories": advisories, "custom_table": custom}


def place_adjust_frame(
    input_path: str,
    variables: List[str],
    year: int,
    areas: str = "county_metro",
    replicates_path: Optional[str] = None,
    output_path: Optional[str] = None,
    replicates_output_path: Optional[str] = None,
    rpp_year: Optional[int] = None,
) -> Dict[str, Any]:
    """Express dollar variables of an analysis frame in national-average purchasing
    power with BEA Regional Price Parities, row by row (value * 100 / RPP).

    Args:
        input_path: Analysis frame from build_analysis_frame (GEOID per row).
        variables: Dollar variables to adjust, e.g. ["income"]; their _se is
            scaled by the same factor (_cv is unchanged).
        year: The dollars' year (ACS 5-year: the release end year) and RPP year.
        areas: "county_metro" (counties: the MSA RPP for counties in a
            metropolitan CBSA, else the state's nonmetropolitan-portion RPP;
            membership from Census CBSA boundaries of BEA's delineation) or
            "state" (the state RPP for every row).
        replicates_path: Replicate file from build_analysis_frame; the rows of
            the adjusted variables are scaled too (written to replicates_output_path).
        rpp_year: RPP year when it differs from year (default: year); noted in the result.
    The RPP is treated as fixed: its own estimation error is not in the adjusted
    SEs, MOEs or replicates. Never applied automatically. A manifest can declare the
    same adjustment as an "adjustments" step (run_manifest).
    Example: place_adjust_frame("frame.parquet", ["income"], 2024)
    """
    from census import rpp

    if areas not in _RPP_AREAS:
        return _fail(errors.INVALID_INPUT, f"areas must be one of {_RPP_AREAS}.")
    if isinstance(variables, str):
        variables = [variables]
    try:
        reps = _read_replicates(replicates_path) if replicates_path else None
        res = place_adjust(_load_frame(input_path), variables, year, areas=areas, replicates=reps,
                           rpp_year=rpp_year)
        rep_out = None
        if replicates_path:
            rep_out = _write_frame(res["replicates"], replicates_output_path
                                   or os.path.splitext(replicates_path)[0] + "_rpp.parquet", "replicates_rpp.parquet")
    except BundledDataIntegrityError as exc:
        return integrity_error_result(exc)
    except (*_ERRORS, rpp.RPPUnavailable) as exc:
        return _fail(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}")
    frame = res["frame"]
    path = _write_frame(frame, output_path or os.path.splitext(input_path)[0] + "_rpp.parquet", "frame_rpp.parquet")
    return _ok(f"Adjusted {', '.join(variables)} for {len(frame)} rows with {len(res['parity'])} RPP area(s).",
               "fit_model or make_map with input_path=output_path"
               + (" and replicates_path=replicates_output_path" if rep_out else "")
               + "; state in results that a regional price parity adjustment was applied",
               output_path=path, replicates_output_path=rep_out, areas=areas, year=int(year),
               rpp_year=res["rpp_year"], rpp_areas=res["rpp_areas"], notes=res["notes"],
               advisories=res["advisories"], custom_table=res["custom_table"], applied_by_default=False)


def make_web_map(
    input_path: str,
    column: str,
    level: str,
    year: int,
    se_col: Optional[str] = None,
    moe_col: Optional[str] = None,
    scheme: str = "quantiles",
    output_path: Optional[str] = None,
    product: str = "5-year",
) -> Dict[str, Any]:
    """Self-contained interactive HTML map (no external servers or tiles): hover
    for each area's estimate, MOE, CV, and reliability; legend reports class
    separability like the journal maps.

    product: "5-year" (default) or "1-year"; named in the map's data citation.
    Example: make_web_map("frame.parquet", "poverty_rate", "county", 2023, se_col="poverty_rate_se")
    """
    from census.web_maps import web_choropleth
    from census.workflows_geo import _with_boundaries

    try:
        gdf = _with_boundaries(_load_frame(input_path), level, year)
        path = output_path or f"{_out_dir(None, 'maps')}/{column}_{level}_{year}.html"
        res = web_choropleth(gdf, column, se_col=se_col, moe_col=moe_col, scheme=scheme, out_path=path,
                             product=product)
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    return _ok(f"Interactive map of {column} written ({res.get('n_features')} areas, {res.get('bytes')} bytes).",
               "open the HTML file in a browser; it makes no network requests",
               **{k: (str(v) if k == "path" else v) for k, v in dict(res).items()})
