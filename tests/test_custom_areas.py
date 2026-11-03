# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allocation of tracts / block groups to user polygons (synthetic geometry, offline)."""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from census import custom_areas as ca

CRS = "EPSG:5070"  # equal-area meters, so areas are exact for boxes


@pytest.fixture
def units():
    # Two tracts side by side, each 1000 x 1000 m.
    return gpd.GeoDataFrame(
        {"GEOID": ["10001000100", "10001000200"]},
        geometry=[box(0, 0, 1000, 1000), box(1000, 0, 2000, 1000)], crs=CRS,
    )


@pytest.fixture
def areas():
    # "west" covers tract 1 and the western quarter of tract 2; "east" the rest.
    return gpd.GeoDataFrame(
        {"name": ["west", "east"]},
        geometry=[box(0, 0, 1250, 1000), box(1250, 0, 2000, 1000)], crs=CRS,
    )


@pytest.fixture
def blocks():
    # Tract 2 has 100 people in its western quarter and 300 in the east.
    pts = [Point(500, 500), Point(1100, 500), Point(1600, 500), Point(1900, 500)]
    return gpd.GeoDataFrame({
        "block_geoid": ["100010001001000", "100010002001000", "100010002001001", "100010002001002"],
        "population": [500, 100, 150, 150],
        "housing_units": [200, 10, 60, 30],
    }, geometry=pts, crs=CRS)


def _w(cw):
    return cw.set_index(["source_geoid", "target_geoid"])["weight"]


def test_area_allocation(units, areas):
    cw = ca.allocate_units(areas, units, method="area")
    w = _w(cw)
    assert w[("10001000100", "west")] == pytest.approx(1.0)
    assert w[("10001000200", "west")] == pytest.approx(0.25)
    assert w[("10001000200", "east")] == pytest.approx(0.75)
    assert cw.attrs["partial_units"] == ["10001000200"]
    assert cw.attrs["whole_units"] == 1
    assert "area" in cw.attrs["assumption"]


def test_population_allocation(areas, blocks, units):
    cw = ca.allocate_units(areas, units, method="population", blocks=blocks)
    w = _w(cw)
    assert w[("10001000200", "west")] == pytest.approx(100 / 400)
    assert w[("10001000200", "east")] == pytest.approx(300 / 400)
    hu = _w(ca.allocate_units(areas, method="housing_units", blocks=blocks))
    assert hu[("10001000200", "east")] == pytest.approx(90 / 100)


def test_block_group_level_and_partial_coverage(blocks):
    area = gpd.GeoDataFrame({"area_id": ["core"]}, geometry=[box(0, 0, 1500, 1000)], crs=CRS)
    cw = ca.allocate_units(area, method="population", unit_level="block group", blocks=blocks)
    w = _w(cw)
    assert w[("100010002001", "core")] == pytest.approx(100 / 400)
    assert cw.attrs["units_outside_any_area_share"]["100010002001"] == pytest.approx(0.75)


def test_read_areas_from_geopackage_and_geojson(tmp_path, areas, units):
    gpkg = tmp_path / "areas.gpkg"
    areas.to_crs("EPSG:4326").to_file(gpkg, driver="GPKG")
    geojson = tmp_path / "areas.geojson"
    areas.to_crs("EPSG:4326").to_file(geojson, driver="GeoJSON")
    for path in (gpkg, geojson):
        cw = ca.allocate_units(str(path), units, method="area", id_col="name")
        assert _w(cw)[("10001000200", "east")] == pytest.approx(0.75, abs=1e-3)


def test_aggregate_partial_and_whole(units, areas):
    cw = ca.allocate_units(areas, units, method="area")
    df = pd.DataFrame({
        "GEOID": ["10001000100", "10001000200"],
        "B01003_001E": [1000.0, 400.0], "B01003_001M": [30.0, 40.0],
    })
    meta = {"B01003_001E": {"label": "Estimate!!Total"}}
    out = ca.aggregate_custom_areas(df, cw, metadata=meta).set_index("GEOID")
    assert out.loc["west", "B01003_001E"] == pytest.approx(1100.0)
    assert out.loc["east", "B01003_001E"] == pytest.approx(300.0)
    assert out.loc["east", "B01003_001M"] == pytest.approx(30.0)
    assert out.attrs["partial_units"] == ["10001000200"]
    mapping = ca.group_mapping(cw)
    assert mapping == {"10001000100": "west", "10001000200": "east"}
    whole = ca.aggregate_custom_areas(df, cw, whole_units=True, metadata=meta)
    rep = whole.attrs["custom_area_assignment"]
    assert rep["units_assigned_partially_inside"] == 1
    assert rep["min_assigned_share"] == pytest.approx(0.75)


def test_errors(units, areas):
    with pytest.raises(ValueError, match="method"):
        ca.allocate_units(areas, units, method="nearest")
    with pytest.raises(ValueError, match="unit polygons"):
        ca.allocate_units(areas, method="area")
    with pytest.raises(ValueError, match="block counts"):
        ca.allocate_units(areas, units, method="population")
    nocrs = gpd.GeoDataFrame({"id": ["a"]}, geometry=[box(0, 0, 1, 1)])
    with pytest.raises(ValueError, match="coordinate reference"):
        ca.read_areas(nocrs)
