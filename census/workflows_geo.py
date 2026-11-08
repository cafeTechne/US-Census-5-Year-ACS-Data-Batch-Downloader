# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 9 geography workflows (boundaries, maps, spatial statistics, custom
areas) shared by the MCP server and the CLI. Same conventions as
census.workflows_inference: plain arguments in, JSON-safe dicts out, with
``summary`` and ``next_step``; failures carry ``error_code``.
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from census import errors
from census.workflows import _jsonable, _load_frame, _records, _write_frame
from census.workflows_inference import _fail, _ok, _out_dir, _read_replicates, result_summary

_LEVELS = ("state", "county", "tract", "block group", "place", "zcta")
_MAP_KINDS = ("estimate", "uncertainty", "significance")
_GEO_ERRORS = (FileNotFoundError, OSError, KeyError, ValueError, TypeError, RuntimeError)


def _release_replicates(reps: Optional[pd.DataFrame], year: int) -> Optional[pd.DataFrame]:
    """Select the requested release from a replicate frame with a Year column
    (PLAN_ROUND_11 C3): never let row order pick among releases."""
    if reps is None or "Year" not in reps.columns:
        return reps
    sel = reps[pd.to_numeric(reps["Year"], errors="coerce") == int(year)]
    if not len(sel):
        raise ValueError(f"replicates have no rows for release {int(year)}")
    return sel


def _per_state(level: str, boundary_year: int) -> bool:
    """Whether boundaries for ``level`` must come from per-state files: tract and
    block group always (smaller downloads), other per-state levels (place,
    county subdivision) only before national cb files exist (2019)."""
    from census.boundaries import FIRST_NATIONAL_SUBSTATE_CB_YEAR, LEVELS, normalize_level

    lvl = normalize_level(level)
    if not LEVELS[lvl]["per_state"]:
        return False
    return lvl in ("tract", "block group") or int(boundary_year) < FIRST_NATIONAL_SUBSTATE_CB_YEAR


def _fetch_boundaries(level: str, acs_year: int, states=None):
    """Boundaries matching an ACS release: the boundary year comes from
    ``boundary_vintage`` (e.g. ZCTA for ACS 2023 -> the 2020 file) and per-state
    files are stitched together when the level has no national file that year."""
    from census.boundaries import boundary_vintage, get_boundaries

    b_year = boundary_vintage(acs_year, level)
    if not _per_state(level, b_year):
        return get_boundaries(level, b_year)
    states = sorted(set(states or ()))
    if not states:
        raise ValueError(f"{level} boundaries for {b_year} are per state; pass state")
    parts = [get_boundaries(level, b_year, state=s) for s in states]
    if len(parts) == 1:
        return parts[0]
    bounds = parts[0].__class__(pd.concat(parts, ignore_index=True), geometry="geometry", crs=parts[0].crs)
    # concat drops attrs; keep the vintage and source for captions and provenance
    bounds.attrs.update({k: v for k, v in parts[0].attrs.items() if k not in ("state", "source_url", "sha256")})
    bounds.attrs["states"] = states
    return bounds


def _with_boundaries(df: pd.DataFrame, level: str, year: int):
    """Attach Census cartographic boundaries matching the ACS release."""
    from census.boundaries import join_boundaries

    if level not in _LEVELS:
        raise ValueError(f"level must be one of {_LEVELS}")
    states = {g[:2] for g in df["GEOID"].astype(str)}
    bounds = _fetch_boundaries(level, year, states)
    return join_boundaries(df, level, acs_year=year, boundaries=bounds, how="inner", on_unmatched="warn")


def _weights_for(gdf, kind: str):
    """Spatial weights; knn / distance weights are built in an equal-area CRS so
    distances are in meters, not degrees."""
    from census import spatial

    if kind in ("knn", "distance") and gdf.crs is not None:
        from census.boundaries import equal_area_crs

        gdf = gdf.to_crs(equal_area_crs(gdf))
    return gdf, spatial.spatial_weights(gdf, kind=kind)


def _paths(fig: Any) -> List[str]:
    paths = fig.paths.values() if isinstance(fig.paths, dict) else fig.paths
    return [str(p) for p in paths]


def get_boundaries(level: str, year: int, state: Optional[str] = None,
                   output_path: Optional[str] = None) -> Dict[str, Any]:
    """Download official Census boundaries matching an ACS 5-year release and save
    them as a GeoPackage (opens in ArcGIS Pro and QGIS).

    Args:
        level: "state", "county", "tract", "block group", "place", or "zcta".
        year: ACS release year (boundary vintage is matched automatically).
        state: 2-digit FIPS; required for tract and block group.
        output_path: .gpkg (default), .parquet, or .shp.
    Example: get_boundaries("tract", 2023, state="10")
    """
    from census import boundaries

    try:
        b_year = boundaries.boundary_vintage(year, level)
        if state is None and not _per_state(level, b_year):
            gdf = boundaries.get_boundaries(level, b_year)
        else:
            gdf = _fetch_boundaries(level, year, [state] if state is not None else None)
        path = boundaries.write_geo(gdf, output_path or str(_out_dir(None, "geo")) + f"/{level.replace(' ', '_')}_{year}_{state or 'us'}.gpkg")
    except _GEO_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    return _ok(f"{len(gdf)} {level} boundaries for {year} written.", "make_map with a data file for the same level and year",
               output_path=str(path), features=len(gdf))


def make_map(
    input_path: str,
    column: str,
    level: str,
    year: int,
    kind: str = "estimate",
    se_col: Optional[str] = None,
    moe_col: Optional[str] = None,
    scheme: str = "quantiles",
    k: int = 5,
    output_dir: Optional[str] = None,
    data_source: str = "",
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Journal-ready map (PDF, SVG, EPS, PNG + values CSV, caption, provenance).

    Args:
        input_path: CSV/Parquet with GEOID and the column (for kind="significance",
            a batch_significance output with GEOID).
        column: Variable to map (estimate maps) or ignored for significance maps.
        level, year: Geography level and ACS release (boundaries matched to it).
        kind: "estimate" (choropleth; legend reports class separability and
            low-reliability areas are hatched), "uncertainty" (CV map), or
            "significance" (increase / decrease / no significant change).
        se_col or moe_col: Standard error or 90% MOE column for reliability.
        scheme: "quantiles" (default, research standard), "natural_breaks",
            "equal_interval", or "separability".
        label: Readable variable name for the legend and caption (default: column).
    Example: make_map("frame.parquet", "poverty_rate", "tract", 2023, se_col="poverty_rate_se")
    """
    from census import maps

    if kind not in _MAP_KINDS:
        return _fail(errors.INVALID_INPUT, f"kind must be one of {_MAP_KINDS}.")
    folder = _out_dir(output_dir, "maps")
    try:
        df = _load_frame(input_path)
        gdf = _with_boundaries(df, level, year)
        common = {"out_dir": folder, "data_source": data_source}
        if kind == "estimate":
            fig = maps.choropleth(gdf, column, se_col=se_col, moe_col=moe_col, scheme=scheme, k=k,
                                  legend_title=label, **common)
        elif kind == "uncertainty":
            fig = maps.uncertainty_map(gdf, column, se_col=se_col, moe_col=moe_col, legend_title=label, **common)
        else:
            fig = maps.significance_map(gdf, df, **common)
    except _GEO_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    return _ok(f"Wrote {kind} map of {column} ({len(gdf)} areas).",
               "include the PDF/SVG and caption; report class separability from the caption",
               figure_paths=_paths(fig), caption=getattr(fig, "caption", ""))


def spatial_autocorrelation(
    input_path: str,
    column: str,
    level: str,
    year: int,
    weights: str = "queen",
    local: bool = False,
    replicates_path: Optional[str] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Moran's I (global) or local clusters (LISA) for one variable.

    Args:
        weights: "queen" (default), "rook", or "knn".
        local: Also return local clusters with multiple-comparison correction
            (two-sided p_value = 2 x p_sim from 9999 permutations, BH,
            alpha 0.10). Simulation: rates at nominal under randomness,
            little power on ~150 areas, and spatially patterned sampling
            error creates spurious clusters (see local_notes).
        replicates_path: Census replicates (build_analysis_frame) for a
            design-based SE of I. That SE is conservative (validated coverage
            0.987 for nominal 0.90).
    Example: spatial_autocorrelation("frame.parquet", "poverty_rate", "tract", 2023)
    """
    from census import spatial

    try:
        gdf, w = _weights_for(_with_boundaries(_load_frame(input_path), level, year), weights)
        reps = _release_replicates(_read_replicates(replicates_path), year)
        res = spatial.morans_i(gdf, column, w, replicates=reps)
        payload: Dict[str, Any] = {"global": res.__dict__ if hasattr(res, "__dict__") else res,
                                   "weights_report": getattr(w, "census_report", {})}
        if local:
            lisa = spatial.local_morans(gdf, column, w, replicates=reps)
            payload["local_output_path"] = _write_frame(lisa, output_path, "local_morans.csv")
            payload["local_preview"] = _records(lisa, 10)
            payload["local_notes"] = list(lisa.attrs.get("notes", []))
    except _GEO_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    i_value = payload["global"].get("I") if isinstance(payload["global"], dict) else None
    return _ok(f"Moran's I for {column}: {i_value}.", "make_map to show the variable; report I with its p-value",
               **_jsonable(payload))


def spatial_regression(
    input_path: str,
    formula: str,
    level: str,
    year: int,
    model: str = "lag",
    weights: str = "queen",
    replicates_path: Optional[str] = None,
    method: Optional[str] = None,
) -> Dict[str, Any]:
    """Spatial lag or spatial error regression (PySAL spreg) with Lagrange
    multiplier diagnostics; replicate SEs when replicates are given.

    Args:
        method: "gm" or "ml". Default: "gm" (GMM) for the lag model, "ml"
            for the error model. Simulation with ACS sampling error in the
            outcome: GMM lag intervals for rho covered 0.90 and 0.89 (rho
            0.3, 0.6) for nominal 0.90, ML lag only 0.48 and 0.003. Error
            model: lambda is attenuated (either method); beta intervals
            hold. See the result notes.

    Example: spatial_regression("frame.parquet", "poverty_rate ~ median_rent", "tract", 2023, model="error")
    """
    from census import spatial

    try:
        gdf, w = _weights_for(_with_boundaries(_load_frame(input_path), level, year), weights)
        res = spatial.spatial_regression(formula, gdf, w, model=model, method=method, replicates=_release_replicates(_read_replicates(replicates_path), year))
    except _GEO_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    out = res.to_dict()
    return _ok(result_summary(res), "make_table or report coefficients with the LM diagnostics",
               **{k: v for k, v in out.items() if k != "summary"})


def spatial_small_area_estimates(
    input_path: str,
    formula: str,
    se_col: str,
    level: str,
    year: int,
    weights: str = "queen",
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Spatial Fay-Herriot: small-area estimates that borrow strength from
    neighbors and covariates (validated: 90% intervals cover 0.891; reproduces
    the published R sae grapes example).

    Example: spatial_small_area_estimates("frame.parquet", "poverty_rate ~ unemployment_rate", "poverty_rate_se", "tract", 2023)
    """
    from census.sae_spatial import spatial_fay_herriot

    try:
        gdf, w = _weights_for(_with_boundaries(_load_frame(input_path), level, year), weights)
        res = spatial_fay_herriot(formula, pd.DataFrame(gdf.drop(columns="geometry")), se_col, w)
    except _GEO_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    path = _write_frame(res.estimates, output_path, "spatial_small_area_estimates.csv")
    rho = res.provenance.get("rho")
    return _ok(f"Spatial Fay-Herriot for {len(res.estimates)} areas; rho={rho}, sigma2_u={res.sigma2_u:.4g}.",
               "make_map of the EBLUP column; report rho and the MSE-based intervals",
               output_path=path, rho=rho, sigma2_u=res.sigma2_u, coefficients=res.coefficients,
               notes=res.notes, preview=_records(res.estimates, 10))


def custom_area_mapping(
    areas_path: str,
    unit_level: str = "tract",
    method: str = "area",
    id_col: Optional[str] = None,
    year: int = 2023,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Allocate tracts or block groups to your own polygons (neighborhoods,
    catchments) from a GeoPackage, GeoJSON, or shapefile.

    Args:
        method: "area" (overlap share) or "population"/"housing_units"
            (decennial block weights; more accurate where people are unevenly spread).
    Returns a mapping file for aggregate_geographies; partial overlaps and the
    allocation assumption are reported.
    Example: custom_area_mapping("neighborhoods.gpkg", "tract", "population")
    """
    from census.custom_areas import allocate_units, group_mapping, load_block_points, mapping_report, read_areas

    try:
        areas = read_areas(areas_path, id_col=id_col)
        state_fips = None
        units = blocks = None
        if unit_level in ("tract", "block group"):
            counties = _fetch_boundaries("county", year)
            hits = counties.to_crs(areas.crs).sjoin(areas, predicate="intersects")
            state_fips = sorted({str(g)[:2] for g in hits["GEOID"]})
            units = _fetch_boundaries(unit_level, year, state_fips)
            if method != "area":
                # decennial blocks matching the release's tract vintage (2010 tracts through ACS 2019)
                block_year = 2020 if int(year) >= 2020 else 2010
                parts = [load_block_points(s, block_year) for s in state_fips]
                blocks = parts[0] if len(parts) == 1 else parts[0].__class__(
                    pd.concat(parts, ignore_index=True), geometry="geometry", crs=parts[0].crs)
        allocation = allocate_units(areas, units, method=method, unit_level=unit_level, blocks=blocks)
        mapping = group_mapping(allocation)
        report = mapping_report(allocation)
    except _GEO_ERRORS as exc:
        return _fail(errors.INVALID_INPUT, str(exc))
    path = _write_frame(allocation, output_path, "custom_area_allocation.csv")
    return _ok(f"Allocated {len(mapping)} {unit_level}(s) to {allocation.iloc[:, 0].nunique()} area(s) by {method}.",
               "aggregate_geographies with groups from this mapping (GEOID -> area)",
               output_path=path, groups=mapping, report=_jsonable(report), states=state_fips)
