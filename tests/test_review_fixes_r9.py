"""Regression tests for the Round 9 code-review findings (offline)."""

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

FIXTURE_GEO = os.path.join(os.path.dirname(__file__), "fixtures", "geo")


@pytest.fixture
def dc_tracts(monkeypatch):
    from census import boundaries

    monkeypatch.setenv("CENSUS_GEO_DIR", FIXTURE_GEO)
    return boundaries.get_boundaries("tract", 2023, state="11")


def _fake_bounds(level, year, state=None, **_):
    import geopandas as gpd
    from shapely.geometry import box

    g = gpd.GeoDataFrame({"GEOID": [f"{state or '06'}001"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4269")
    g.attrs.update(level=level, year=year, state=state)
    return g


# numpy 2: float() on 1-element arrays from spreg
def test_scalar_accepts_one_element_arrays():
    from census.spatial import _scalar

    assert _scalar(np.array([[0.25]])) == 0.25
    assert _scalar(np.float64(1.5)) == 1.5


# 1. custom_area_mapping passes block points for population / housing weights
def test_custom_area_mapping_loads_blocks():
    from census import custom_areas, workflows_geo

    counties = _fake_bounds("county", 2023)
    blocks = pd.DataFrame({"block_geoid": ["060010001001000"], "population": [5]})
    alloc = pd.DataFrame({"source_geoid": ["06001000100"], "target_geoid": ["A"], "weight": [1.0], "part_area": [1.0]})
    with patch.object(workflows_geo, "_fetch_boundaries", side_effect=lambda lvl, y, st=None: counties), \
            patch.object(custom_areas, "read_areas", return_value=counties.rename(columns={"GEOID": "area_id"})), \
            patch.object(custom_areas, "load_block_points", return_value=blocks) as lbp, \
            patch.object(custom_areas, "allocate_units", return_value=alloc) as au:
        out = workflows_geo.custom_area_mapping("areas.gpkg", "tract", "population", year=2018)
    assert out["success"], out
    lbp.assert_called_once_with("06", 2010)  # ACS 2018 uses 2010 tracts -> 2010 blocks
    assert au.call_args.kwargs["blocks"] is blocks


# 2. boundary year from boundary_vintage; per-state place files before 2019
def test_fetch_boundaries_vintage_and_per_state():
    from census import boundaries, workflows_geo

    with patch.object(boundaries, "get_boundaries", side_effect=_fake_bounds) as gb:
        workflows_geo._fetch_boundaries("zcta", 2023)
        assert gb.call_args_list[-1].args[:2] == ("zcta", 2020)
        out = workflows_geo._fetch_boundaries("place", 2015, {"41", "06"})
        assert [c.kwargs["state"] for c in gb.call_args_list[-2:]] == ["06", "41"]
        assert out.attrs["states"] == ["06", "41"] and len(out) == 2
        workflows_geo._fetch_boundaries("place", 2021, {"06"})
        assert gb.call_args_list[-1].args == ("place", 2021) and "state" not in gb.call_args_list[-1].kwargs


# 3. knn / distance weights are built in an equal-area CRS
def test_weights_for_projects_knn(dc_tracts):
    from census.workflows_geo import _weights_for

    g, w = _weights_for(dc_tracts, "knn")
    assert not g.crs.is_geographic and w.n == len(dc_tracts)
    g2, _ = _weights_for(dc_tracts, "queen")
    assert g2.crs == dc_tracts.crs


# 4. block-group county lists per requested release
def test_block_group_counties_per_year():
    from census.pipeline import build_entities, probe_block_group_availability

    lists = {2021: ["001", "003"], 2022: ["110", "120"]}
    client = MagicMock()
    client.list_geographies.side_effect = lambda y, lvl, state_code: [{"county": c} for c in lists[y]]
    ents = build_entities("block group", "09", client=client, years=[2021, 2022])
    by = {e.county_code: e.years for e in ents}
    assert by == {"001": (2021,), "003": (2021,), "110": (2022,), "120": (2022,)}
    assert sorted(c.args[0] for c in client.list_geographies.call_args_list) == [2021, 2022]
    client.build_request.return_value = ("e", {})
    client._get_json.return_value = [["B01001_001E"], ["5"]]
    probe_block_group_availability(client, "B01001", [2021, 2022], ents)
    used = [c.kwargs["county_code"] for c in client.build_request.call_args_list]
    assert used == ["001", "110"]
    # same list every year -> unrestricted entities
    client.list_geographies.side_effect = lambda y, lvl, state_code: [{"county": "001"}]
    assert [e.years for e in build_entities("block group", "10", client=client, years=[2021, 2022])] == [None]


# 5. cross-state pieces are counted once, by the source tract's own state
def test_tract_weights_cross_state_piece():
    from census import block_weights as bw

    def rel(rows):
        return pd.DataFrame(rows, columns=["block_2010", "block_2020", "tract_2010", "tract_2020",
                                           "arealand_int", "areawater_int"])

    # Delaware tract 10001000100 has one piece that became Maryland 2020 tract 24001000100.
    cross = ["100010001001001", "240010001001001", "10001000100", "24001000100", 10.0, 0.0]
    de = ["100010001001002", "100010001001002", "10001000100", "10001000100", 30.0, 0.0]
    md = ["240010001001003", "240010001001003", "24001000100", "24001000100", 20.0, 0.0]
    files = {"10": rel([cross, de]), "24": rel([cross, md])}
    counts = pd.DataFrame({"block_geoid": ["100010001001001", "100010001001002", "240010001001003"],
                           "population": [10.0, 30.0, 7.0], "housing_units": [1.0, 1.0, 1.0]})
    county_calls = {}

    def fake_counts(st, sv, counties=None, **_):
        county_calls[st] = list(counties)
        return counts

    with patch.object(bw, "load_block_relationships", side_effect=lambda st, session=None: files[st]), \
            patch.object(bw, "load_block_counts", side_effect=fake_counts):
        cw = bw.tract_weights(["10", "24"], weight="population")
    w = cw.set_index(["source_geoid", "target_geoid"])["weight"]
    assert w[("10001000100", "24001000100")] == pytest.approx(0.25)
    assert w[("10001000100", "10001000100")] == pytest.approx(0.75)
    assert w[("24001000100", "24001000100")] == pytest.approx(1.0)
    assert not cw.duplicated(["source_geoid", "target_geoid"]).any()
    assert county_calls == {"10": ["001"], "24": ["001"]}


# 6. failed validation keeps its own summary / next step
def test_guided_keeps_non_error_failure_summary():
    from census.workflows import _guided

    @_guided(lambda o: "Validation failed: x.", lambda o: "inspect failures")
    def f():
        return {"success": False, "checks_summary": {"failed": ["x"]}}

    out = f()
    assert out["summary"] == "Validation failed: x." and out["next_step"] == "inspect failures"

    @_guided(lambda o: "ok", "next")
    def g():
        return {"success": False, "error": "boom", "error_code": "X"}

    assert g()["summary"] == "boom"


# 7. block-group advisories
def test_block_group_boundary_advisories():
    from census.inflation import boundary_shift_advisory

    assert "block group" in boundary_shift_advisory(2019, 2020, "block group").lower()
    assert "CONNECTICUT" in boundary_shift_advisory(2021, 2022, "block group")


# 8. estimate maps without SE / MOE
def test_make_map_without_se(dc_tracts, tmp_path, monkeypatch):
    from census import workflows_geo

    monkeypatch.setenv("CENSUS_GEO_DIR", FIXTURE_GEO)
    path = tmp_path / "f.csv"
    pd.DataFrame({"GEOID": dc_tracts["GEOID"].astype(str),
                  "rate": np.linspace(1, 30, len(dc_tracts))}).to_csv(path, index=False)
    out = workflows_geo.make_map(str(path), "rate", "tract", 2023, output_dir=str(tmp_path / "maps"))
    assert out["success"], out
    assert "reliability not shown" in out["caption"] and "hatching" not in out["caption"]


# 9. a weights file is always reported as user-supplied
def test_weights_file_is_user_supplied(tmp_path):
    from census import geo_crosswalk as gc

    path = tmp_path / "w.csv"
    path.write_text("source_geoid,target_geoid,weight\n01001020500,01001020501,1\n")
    assert gc.tract_crosswalk(weights_file=str(path)).attrs["weight_type"] == "user-supplied"
    cw = gc.tract_crosswalk(weight="housing_units", weights_file=str(path))
    assert cw.attrs["weight_type"] == "user-supplied" and cw.attrs["weight_label"] == "housing_units"


# 10. download_table plans once and hands the plan to run_batch
def test_download_table_plans_once(tmp_path):
    from census import pipeline, workflows

    plan = {"success": True, "would_block": False}
    with patch.object(pipeline, "plan_extraction", return_value=plan) as pe, \
            patch.object(pipeline, "run_batch", return_value={"success": True}) as rb:
        res = workflows.download_table("B01003", "state", state_code="10", years=[2022], output_dir=str(tmp_path))
    assert pe.call_count == 1, res
    assert rb.call_args.kwargs["plan"] is plan
    with patch.object(pipeline, "plan_extraction", side_effect=AssertionError("planned twice")):
        out = pipeline.run_batch("B01003", [], [2022], str(tmp_path), plan={"success": False, "error": "x"})
    assert out == {"success": False, "error": "x"}
