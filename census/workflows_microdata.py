# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 10 workflows (PUMS microdata, research designs, place price levels,
interactive maps) shared by the MCP server and the CLI. Same conventions as
census.workflows_inference: plain arguments in, JSON-safe dicts out, with
``summary`` and ``next_step``; failures carry ``error_code``.
"""

import re
from typing import Any, Dict, List, Optional

from census import errors
from census.workflows import _load_frame, _records, _write_frame
from census.workflows_inference import _fail, _ok, _out_dir, _read_replicates

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
    return _ok(f"PUMS {statistic} for {len(out)} cell(s); {flagged} flagged (fewer than 30 records, "
               "CV over 30%, or undefined uncertainty).",
               "report estimates with their MOE; treat flagged cells with caution",
               output_path=path, preview=_records(out, 20))


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
    return _ok(res.summary(), "report coefficients with the SE named in uncertainty_sources",
               **{k: v for k, v in out.items() if k != "summary"})


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
    return _ok(res.summary(), "report coefficients with df and the SE named in uncertainty_sources",
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
    return _ok(res.summary(), "report the DiD estimate with its interval and the parallel-trends result",
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
    except _ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    return _ok(f"{value:,.0f} in {geography} ({year}) = {adjusted:,.0f} at national-average prices"
               + (f" in {base_year} dollars" if base_year else "") + ".",
               "state in results that a regional price parity adjustment was applied",
               value=value, adjusted_value=adjusted, geography=geography, year=year, base_year=base_year,
               adjustment=described)


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
