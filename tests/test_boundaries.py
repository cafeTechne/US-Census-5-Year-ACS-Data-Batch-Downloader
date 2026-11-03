# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for census.boundaries (PLAN_ROUND_9.md, workstream A).

Uses tests/fixtures/geo/2023/cb_2023_11_tract_500k.zip (the real 2023
cartographic boundary file for DC tracts, 46 KB) with its SHA-256 sidecar,
plus small constructed geometries.
"""

import os
import shutil
import warnings

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from census import boundaries as B

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "geo")
DC = os.path.join(FIX, "2023", "cb_2023_11_tract_500k.zip")


class NoNetwork:
    def get(self, *a, **k):
        raise AssertionError("network access attempted")


class FakeResp:
    status_code = 200
    headers = {"Content-Type": "application/zip"}

    def __init__(self, content):
        self.content = content


class FakeSession:
    def __init__(self, content):
        self.content, self.urls = content, []

    def get(self, url, **k):
        self.urls.append(url)
        return FakeResp(self.content)


def test_url_patterns():
    g = "https://www2.census.gov/geo/tiger/"
    assert B.boundary_url("county", 2013) == g + "GENZ2013/cb_2013_us_county_500k.zip"
    assert B.boundary_url("tract", 2019, "6") == g + "GENZ2019/shp/cb_2019_06_tract_500k.zip"
    assert B.boundary_url("tract", 2022) == g + "GENZ2022/shp/cb_2022_us_tract_500k.zip"
    assert B.boundary_url("bg", 2023, "11") == g + "GENZ2023/shp/cb_2023_11_bg_500k.zip"
    assert B.boundary_url("zcta", 2019) == g + "GENZ2019/shp/cb_2019_us_zcta510_500k.zip"
    assert B.boundary_url("zcta", 2020) == g + "GENZ2020/shp/cb_2020_us_zcta520_500k.zip"
    assert B.boundary_url("msa", 2023) == g + "GENZ2023/shp/cb_2023_us_cbsa_500k.zip"
    assert B.boundary_url("tract", 2023, "11", "tiger") == g + "TIGER2023/TRACT/tl_2023_11_tract.zip"
    assert B.boundary_url("zcta", 2019, detail="tiger") == g + "TIGER2019/ZCTA5/tl_2019_us_zcta510.zip"
    assert B.boundary_url("zcta", 2023, detail="tiger") == g + "TIGER2023/ZCTA520/tl_2023_us_zcta520.zip"
    assert B.boundary_url("state", 2023, detail="tiger") == g + "TIGER2023/STATE/tl_2023_us_state.zip"
    assert B.boundary_url("place", 2023, state="District of Columbia") == \
        g + "GENZ2023/shp/cb_2023_11_place_500k.zip"
    with pytest.raises(ValueError, match="national cb tract"):
        B.boundary_url("tract", 2018)
    with pytest.raises(ValueError, match="per state"):
        B.boundary_url("tract", 2023, detail="tiger")
    with pytest.raises(ValueError, match="no cb ZCTA"):
        B.boundary_url("zcta", 2023)
    with pytest.raises(ValueError):
        B.boundary_url("block", 2023)


def test_registry_patterns_match_verified_urls():
    for rel, status in B.VERIFIED_URLS:
        if status != 200:
            continue
        parts = rel.rsplit("/", 1)[-1][:-4].split("_")
        detail = "cb" if parts[0] == "cb" else "tiger"
        year, area, lvl = int(parts[1]), parts[2], parts[3]
        lvl = {"zcta510": "zcta", "zcta520": "zcta"}.get(lvl, lvl)
        url = B.boundary_url(lvl, year, None if area == "us" else area, detail)
        assert url == B.BASE_URL + "/" + rel


def test_boundary_vintage():
    assert B.boundary_vintage(2019, "tract") == 2019
    assert B.boundary_vintage(2023, "tract") == 2023
    assert B.boundary_vintage(2023, "zcta") == 2020
    assert B.boundary_vintage(2017, "zcta") == 2017
    assert B.boundary_vintage(2023, "zcta", "tiger") == 2023
    assert B.boundary_vintage(2012, "county", "tiger") == 2012
    with pytest.raises(ValueError, match="detail='tiger'"):
        B.boundary_vintage(2012, "county")
    with pytest.raises(ValueError):
        B.boundary_vintage(2009, "county", "tiger")


def test_vintage_mismatch_is_an_error():
    with pytest.raises(B.BoundaryVintageError, match="2010 Census tracts"):
        B.check_boundary_vintage(2019, 2020, "tract")
    with pytest.raises(B.BoundaryVintageError, match="Connecticut"):
        B.check_boundary_vintage(2022, 2021, "county")
    with pytest.raises(B.BoundaryVintageError, match="use 2023"):
        B.check_boundary_vintage(2023, 2022, "tract")
    assert B.check_boundary_vintage(2023, 2020, "zcta") == 2020


def test_get_boundaries_from_cache_offline():
    g = B.get_boundaries("tract", 2023, "11", cache_dir=FIX, session=NoNetwork())
    assert len(g) == 206
    assert g.crs.to_epsg() == 4269
    assert g["GEOID"].is_monotonic_increasing and g["GEOID"].str.len().eq(11).all()
    assert g.attrs["year"] == 2023 and g.attrs["level"] == "tract" and g.attrs["detail"] == "cb"
    assert g.attrs["sha256"] == "d410e86d168030191edf44d22d5ba0d9cd89cc723c652740bb36bf5e174aeeb6"
    assert "2023 cartographic boundary" in g.attrs["description"]


def test_corrupt_cache_redownloads(tmp_path):
    d = tmp_path / "2023"
    d.mkdir()
    shutil.copy(DC, d)
    shutil.copy(DC + ".json", d)
    target = d / "cb_2023_11_tract_500k.zip"
    target.write_bytes(b"PK corrupted")
    with open(DC, "rb") as f:
        raw = f.read()
    sess = FakeSession(raw)
    g = B.get_boundaries("tract", 2023, "11", cache_dir=str(tmp_path), session=sess)
    assert sess.urls == [B.boundary_url("tract", 2023, "11")]
    assert len(g) == 206 and target.read_bytes() == raw


def test_download_rejects_non_zip(tmp_path):
    with pytest.raises(B.BoundaryError, match="zip"):
        B.get_boundaries("tract", 2023, "11", cache_dir=str(tmp_path), session=FakeSession(b"<html>"))


def _frame():
    g = B.get_boundaries("tract", 2023, "11", cache_dir=FIX, session=NoNetwork())
    ids = g["GEOID"].tolist()
    df = pd.DataFrame({"GEOID": [int(ids[0]), ids[1], ids[2], "11001999999"],
                       "B19013_001E": [1.0, 2.0, 3.0, 4.0]})
    return g, df


def test_join_reports_unmatched_both_sides():
    g, df = _frame()
    with pytest.warns(UserWarning, match="1 data GEOIDs"):
        out = B.join_boundaries(df, "tract", acs_year=2023, boundaries=g)
    rep = out.attrs["join_report"]
    assert rep["matched"] == 3 and rep["unmatched_data"] == ["11001999999"]
    assert len(rep["unmatched_boundaries"]) == 203
    assert len(out) == 206 and out["B19013_001E"].notna().sum() == 3
    assert isinstance(out, gpd.GeoDataFrame) and out.attrs["year"] == 2023
    inner = B.join_boundaries(df, "tract", 2023, boundaries=g, how="inner", on_unmatched="ignore")
    assert len(inner) == 3
    with pytest.raises(ValueError, match="no 2023 tract boundary"):
        B.join_boundaries(df, "tract", 2023, boundaries=g, on_unmatched="raise")


def test_join_vintage_mismatch_raises():
    g, df = _frame()
    with pytest.raises(B.BoundaryVintageError):
        B.join_boundaries(df, "tract", acs_year=2019, boundaries=g)
    df.attrs["year"] = 2021
    with pytest.raises(B.BoundaryVintageError):
        B.join_boundaries(df, "tract", boundaries=g)


def test_normalize_geoids():
    s = B.normalize_geoids([1001, "1400000US11001000201", 6037.0, None], "county").tolist()
    assert s[0] == "01001" and s[1] == "11001000201" and s[2] == "06037" and s[3] is None


def _toy():
    return gpd.GeoDataFrame({"GEOID": ["20001", "20003"],
                             "B19013_001E_moe": [1.0, 2.0], "B19013_001E_cv": [3.0, 4.0]},
                            geometry=[box(-100, 40, -99, 41), box(-99, 40, -98, 41)], crs="EPSG:4269")


@pytest.mark.parametrize("fmt", ["gpkg", "parquet"])
def test_write_geo_roundtrip(tmp_path, fmt):
    p = B.write_geo(_toy(), tmp_path / "out", format=fmt)
    assert p.endswith("." + fmt)
    back = gpd.read_parquet(p) if fmt == "parquet" else gpd.read_file(p)  # GDAL read-back
    assert list(back["GEOID"]) == ["20001", "20003"]
    assert "B19013_001E_moe" in back.columns and back.crs.to_epsg() == 4269


def test_write_geo_default_is_gpkg_and_shapefile_warns(tmp_path):
    assert B.write_geo(_toy(), tmp_path / "x").endswith(".gpkg")
    with pytest.warns(UserWarning, match="COLLISIONS"):
        p = B.write_geo(_toy(), tmp_path / "x.shp")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert len(gpd.read_file(p)) == 2


def _one(geoid, x, y):
    return gpd.GeoDataFrame({"GEOID": [geoid]}, geometry=[box(x, y, x + 1, y + 1)], crs="EPSG:4269")


def test_equal_area_crs():
    assert B.equal_area_crs(_one("20001", -100, 40)).to_epsg() == 5070
    assert B.equal_area_crs(_one("02020", -150, 61)).to_epsg() == 3338
    assert "Hawaii" in B.equal_area_crs(_one("15003", -158, 21)).name
    assert "Albers" in B.equal_area_crs(_one("72127", -66.2, 18.2)).to_wkt()
    mixed = gpd.GeoDataFrame(pd.concat([_one("20001", -100, 40), _one("02020", -150, 61)]), crs="EPSG:4269")
    assert B.equal_area_crs(mixed).to_epsg() == 5070
    nostate = gpd.GeoDataFrame({"x": [1]}, geometry=[box(-150, 61, -149, 62)], crs="EPSG:4269")
    assert B.equal_area_crs(nostate).to_epsg() == 3338
