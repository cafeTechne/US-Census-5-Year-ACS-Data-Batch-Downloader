# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allocate tracts or block groups to user-drawn areas (neighborhoods, catchments).

A user supplies polygons (GeoPackage, GeoJSON, shapefile, or a GeoDataFrame).
Each census unit (tract or block group) is allocated to the areas it overlaps:

* ``method="area"``: share of the unit's polygon area inside each area,
  computed in an equal-area projection (EPSG:5070 by default). Assumes the
  quantity is spread evenly over the unit's area (water included unless the
  unit polygons exclude it).
* ``method="population"`` / ``"housing_units"``: share of the unit's 2020
  (or 2010) decennial block population or housing units whose block internal
  point falls inside each area (blocks from TIGER/Line ``tabblock20`` files,
  which carry ``POP20``/``HOUSING20``, or user-supplied block points). Assumes
  the ACS quantity is distributed like the decennial count across the unit's
  blocks; blocks are assigned whole.

The result is a crosswalk ``source_geoid, target_geoid, weight, part_area``
(target = area id). Units entirely inside one area have weight 1; partial
units have weights < 1 and are listed in ``attrs["partial_units"]``.

Use it with:

* :func:`census.geo_crosswalk.apportion` (or :func:`aggregate_custom_areas`)
  for weighted sums: estimate = sum(w X), MOE = sqrt(sum((w MOE)^2)); weights
  are treated as exact and components as independent;
* :func:`group_mapping` -> ``census.aggregate.aggregate_geographies`` when each
  unit should be assigned whole (largest share), which allows exact replicate
  MOEs; the units whose assignment drops part of them are reported.
"""

import os
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .block_weights import BlockDataError, _cached_or_fetch, _get, _state, block_cache_dir

METHODS = ("area", "population", "housing_units")
EQUAL_AREA_CRS = "EPSG:5070"  # NAD83 / Conus Albers
UNIT_LENGTH = {"tract": 11, "block group": 12}
BLOCK_FILES = {
    2020: ("https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_{state}_tabblock20.zip",
           "GEOID20", "POP20", "HOUSING20"),
    2010: ("https://www2.census.gov/geo/tiger/TIGER2010BLKPOPHU/tabblock2010_{state}_pophu.zip",
           "BLOCKID10", "POP10", "HOUSING10"),
}
ASSUMPTION = {
    "area": "partial units are split by share of polygon area (uniform density within each unit)",
    "population": "partial units are split by the share of their decennial block population inside "
                  "each area (ACS counts assumed distributed like block population; blocks assigned whole "
                  "by internal point)",
    "housing_units": "partial units are split by the share of their decennial block housing units inside "
                     "each area (blocks assigned whole by internal point)",
}


def read_areas(areas: Any, id_col: Optional[str] = None, layer: Optional[str] = None) -> Any:
    """Load user polygons as a GeoDataFrame with an ``area_id`` column.

    ``areas``: path (GeoPackage, GeoJSON, shapefile, or anything GDAL reads)
    or a GeoDataFrame. ``id_col`` names the identifier column (default: the
    first of ``area_id, id, name, NAME``, else the row number).
    """
    import geopandas as gpd

    gdf = gpd.read_file(areas, layer=layer) if isinstance(areas, (str, os.PathLike)) else areas.copy()
    if not isinstance(gdf, gpd.GeoDataFrame):
        raise TypeError("areas must be a path or a GeoDataFrame")
    if gdf.crs is None:
        raise ValueError("areas have no coordinate reference system; set one (e.g. EPSG:4326)")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        raise ValueError("areas contain no polygons")
    if id_col is None:
        id_col = next((c for c in ("area_id", "id", "name", "NAME") if c in gdf.columns), None)
    ids = gdf[id_col].astype(str) if id_col else pd.Series([str(i) for i in range(len(gdf))], index=gdf.index)
    if ids.duplicated().any():
        gdf = gdf.assign(area_id=ids.values).dissolve(by="area_id", as_index=False)
    else:
        gdf = gdf.assign(area_id=ids.values)
    gdf["geometry"] = gdf.geometry.make_valid()
    return gdf[["area_id", "geometry"]].reset_index(drop=True)


def load_block_points(state: Any, year: int = 2020, session: Any = None) -> Any:
    """Decennial block internal points with ``block_geoid, population, housing_units``.

    Downloads the TIGER/Line block file for one state once (cached with a
    SHA-256 sidecar under :func:`census.block_weights.block_cache_dir`).
    """
    import geopandas as gpd

    if year not in BLOCK_FILES:
        raise ValueError(f"year must be one of {sorted(BLOCK_FILES)}")
    st = _state(state)
    template, gcol, pcol, hcol = BLOCK_FILES[year]
    url = template.format(state=st)
    path = os.path.join(block_cache_dir(), os.path.basename(url))
    _cached_or_fetch(path, url, lambda: _get(url, session=session, timeout=600), "application/zip")
    gdf = gpd.read_file(f"zip://{path}")
    if year == 2020 and {"INTPTLAT20", "INTPTLON20"}.issubset(gdf.columns):
        pts = gpd.points_from_xy(pd.to_numeric(gdf["INTPTLON20"]), pd.to_numeric(gdf["INTPTLAT20"]), crs="EPSG:4269")
    else:
        pts = gdf.geometry.representative_point()
    return gpd.GeoDataFrame({
        "block_geoid": gdf[gcol].astype(str).values,
        "population": pd.to_numeric(gdf[pcol], errors="coerce").fillna(0).values,
        "housing_units": pd.to_numeric(gdf[hcol], errors="coerce").fillna(0).values,
    }, geometry=pts, crs=pts.crs if hasattr(pts, "crs") else gdf.crs)


def _report(cw: pd.DataFrame, method: str, unit_level: str, extra: Dict[str, Any]) -> None:
    per_unit = cw.groupby("source_geoid")["weight"].agg(["sum", "size", "max"])
    partial = per_unit[(per_unit["size"] > 1) | (per_unit["sum"] < 1 - 1e-9)]
    cw.attrs.update(
        method=method,
        unit_level=unit_level,
        weight_type=f"custom_area_{method}",
        assumption=ASSUMPTION[method],
        units=int(len(per_unit)),
        whole_units=int(len(per_unit) - len(partial)),
        partial_units=sorted(partial.index),
        units_outside_any_area_share={g: float(1 - v) for g, v in per_unit["sum"].items() if v < 1 - 1e-9},
        notes=[ASSUMPTION[method], f"{len(partial)} of {len(per_unit)} {unit_level}s are split across "
               "areas or only partly inside them"],
        **extra,
    )


def allocate_units(
    areas: Any,
    units: Any = None,
    method: str = "area",
    unit_level: str = "tract",
    unit_id_col: str = "GEOID",
    blocks: Any = None,
    id_col: Optional[str] = None,
    crs: str = EQUAL_AREA_CRS,
    min_weight: float = 0.0,
) -> pd.DataFrame:
    """Crosswalk from tracts / block groups to user areas.

    Args:
        areas: polygons (path or GeoDataFrame; see :func:`read_areas`).
        units: tract or block-group polygons (path or GeoDataFrame with
            ``unit_id_col``); required for ``method="area"``; for block-count
            methods, optional (used only to report units with no blocks).
        method: ``"area"``, ``"population"`` or ``"housing_units"``.
        unit_level: ``"tract"`` (11-digit GEOIDs) or ``"block group"`` (12).
        blocks: block points/polygons with ``block_geoid`` and the count
            column (see :func:`load_block_points`); required for block-count
            methods.
        min_weight: drop slivers with weight below this (e.g. 0.001 to ignore
            boundary noise from mismatched linework); kept weights are not
            rescaled, so dropped mass counts as outside every area.

    Returns ``source_geoid, target_geoid, weight, part_area``; for each unit
    the weights sum to the share of it inside the areas (1 when covered).
    """
    import geopandas as gpd

    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    if unit_level not in UNIT_LENGTH:
        raise ValueError(f"unit_level must be one of {sorted(UNIT_LENGTH)}")
    gareas = read_areas(areas, id_col=id_col).to_crs(crs)
    n = UNIT_LENGTH[unit_level]

    if method == "area":
        if units is None:
            raise ValueError("method='area' needs unit polygons (units=)")
        gunits = gpd.read_file(units) if isinstance(units, (str, os.PathLike)) else units
        if unit_id_col not in gunits.columns:
            raise KeyError(f"units have no {unit_id_col!r} column")
        gunits = gunits[[unit_id_col, "geometry"]].to_crs(crs).copy()
        gunits["source_geoid"] = gunits[unit_id_col].astype(str).str.zfill(n)
        gunits["geometry"] = gunits.geometry.make_valid()
        unit_area = gunits.set_index("source_geoid").geometry.area
        pieces = gpd.overlay(gunits[["source_geoid", "geometry"]], gareas, how="intersection", keep_geom_type=True)
        pieces["part_area"] = pieces.geometry.area
        cw = pieces.groupby(["source_geoid", "area_id"], as_index=False)["part_area"].sum()
        cw["weight"] = cw["part_area"] / cw["source_geoid"].map(unit_area).to_numpy()
        extra: Dict[str, Any] = {"crs": crs}
    else:
        if blocks is None:
            raise ValueError(f"method={method!r} needs block counts (blocks=; see load_block_points)")
        col = method
        b = blocks.copy()
        if col not in b.columns:
            raise KeyError(f"blocks have no {col!r} column")
        b["geometry"] = b.geometry.representative_point() if not (b.geom_type == "Point").all() else b.geometry
        b = b.to_crs(crs)
        b["source_geoid"] = b["block_geoid"].astype(str).str[:n]
        total = b.groupby("source_geoid")[col].sum()
        hit = gpd.sjoin(b[["source_geoid", col, "geometry"]], gareas, how="inner", predicate="within")
        cw = hit.groupby(["source_geoid", "area_id"], as_index=False)[col].sum().rename(columns={col: "part_area"})
        denom = cw["source_geoid"].map(total).to_numpy(dtype=float)
        # Units with no population/housing in their blocks: share of blocks instead.
        nblocks = b.groupby("source_geoid").size()
        nhit = hit.groupby(["source_geoid", "area_id"]).size()
        zero = denom <= 0
        cw["weight"] = np.where(zero, 0.0, cw["part_area"] / np.where(zero, 1, denom))
        if zero.any():
            keys = list(zip(cw.loc[zero, "source_geoid"], cw.loc[zero, "area_id"]))
            cw.loc[zero, "weight"] = [nhit[k] / nblocks[k[0]] for k in keys]
        extra = {"zero_count_units_split_by_block_share": sorted(set(cw.loc[zero, "source_geoid"])), "crs": crs}
        if units is not None:
            gunits = gpd.read_file(units) if isinstance(units, (str, os.PathLike)) else units
            ids = set(gunits[unit_id_col].astype(str).str.zfill(n))
            extra["units_without_blocks"] = sorted(ids - set(total.index))
    cw = cw.rename(columns={"area_id": "target_geoid"})
    cw = cw.loc[cw["weight"] > max(min_weight, 0.0), ["source_geoid", "target_geoid", "weight", "part_area"]]
    cw["weight"] = cw["weight"].clip(upper=1.0)
    cw = cw.sort_values(["source_geoid", "target_geoid"]).reset_index(drop=True)
    _report(cw, method, unit_level, extra)
    return cw


def group_mapping(allocation: pd.DataFrame, min_share: float = 0.5) -> Dict[str, str]:
    """Whole-unit assignment {GEOID: area_id} for ``aggregate_geographies``.

    Each unit goes to the area holding its largest share if that share is at
    least ``min_share``; others are left out. The returned dict's ``report``
    is not available on a dict, so call :func:`mapping_report` for the
    assumption summary.
    """
    best = allocation.sort_values("weight", ascending=False).drop_duplicates("source_geoid")
    best = best[best["weight"] >= min_share]
    return dict(zip(best["source_geoid"], best["target_geoid"]))


def mapping_report(allocation: pd.DataFrame, min_share: float = 0.5) -> Dict[str, Any]:
    """What whole-unit assignment (:func:`group_mapping`) changes vs the weights."""
    best = allocation.sort_values("weight", ascending=False).drop_duplicates("source_geoid")
    kept = best[best["weight"] >= min_share]
    return {
        "assumption": "each unit assigned whole to the area holding its largest share",
        "min_share": min_share,
        "units_assigned": int(len(kept)),
        "units_dropped": sorted(best.loc[best["weight"] < min_share, "source_geoid"]),
        "units_assigned_partially_inside": int((kept["weight"] < 1 - 1e-9).sum()),
        "min_assigned_share": float(kept["weight"].min()) if len(kept) else None,
    }


def aggregate_custom_areas(
    df: pd.DataFrame,
    allocation: pd.DataFrame,
    whole_units: bool = False,
    min_share: float = 0.5,
    **kwargs: Any,
) -> pd.DataFrame:
    """Estimates and MOEs for each custom area.

    ``whole_units=False`` (default): weighted sums with
    :func:`census.geo_crosswalk.apportion` (partial weights; MOE treats
    components as independent). ``whole_units=True``: each unit assigned
    whole (:func:`group_mapping`) and summed with
    :func:`census.aggregate.aggregate_geographies` (replicate MOEs when a
    ``replicate_frame`` is passed in ``kwargs``).
    """
    if whole_units:
        from .aggregate import aggregate_geographies

        mapping = group_mapping(allocation, min_share)
        out = aggregate_geographies(df, mapping, **kwargs)
        out.attrs["custom_area_assignment"] = mapping_report(allocation, min_share)
        return out
    from .geo_crosswalk import apportion

    kwargs.setdefault("min_coverage", 0.0)
    out = apportion(df, allocation, **kwargs)
    out.attrs["notes"] = list(out.attrs.get("notes", [])) + list(allocation.attrs.get("notes", []))
    out.attrs["assumption"] = allocation.attrs.get("assumption")
    out.attrs["partial_units"] = allocation.attrs.get("partial_units", [])
    return out


__all__ = [
    "ASSUMPTION",
    "BlockDataError",
    "METHODS",
    "aggregate_custom_areas",
    "allocate_units",
    "group_mapping",
    "load_block_points",
    "mapping_report",
    "read_areas",
]
