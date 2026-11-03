# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block-based (population / housing-unit) tract crosswalks. Offline: small Delaware fixtures."""

import hashlib
import io
import json
import os
import zipfile

import numpy as np
import pandas as pd
import pytest

from census import block_weights as bw
from census import geo_crosswalk as gc
from census.replicate import _write_cached

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "blocks")
REL_TXT = os.path.join(FIX, "tab2010_tab2020_st10_subset.txt")
FULL_SOURCES = ["10001041100", "10003014300"]  # every piece of these 2010 tracts is in the fixture


def _read(name):
    path = os.path.join(FIX, name)
    with open(path, "rb") as f:
        raw = f.read()
    with open(path + ".json", encoding="utf-8") as f:
        assert json.load(f)["sha256"] == hashlib.sha256(raw).hexdigest()
    return raw


@pytest.fixture(scope="module")
def rel():
    return bw.parse_block_relationships(_read("tab2010_tab2020_st10_subset.txt").decode("utf-8"))


@pytest.fixture(scope="module")
def counts10():
    return pd.read_csv(io.BytesIO(_read("dec2010_pl_blocks_10_subset.csv")), dtype={"block_geoid": str})


@pytest.fixture(scope="module")
def counts20():
    return pd.read_csv(io.BytesIO(_read("dec2020_pl_blocks_10_subset.csv")), dtype={"block_geoid": str})


@pytest.fixture
def block_cache(tmp_path, monkeypatch):
    """A block cache holding the Delaware fixture, offline."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tab2010_tab2020_st10_de.txt", _read("tab2010_tab2020_st10_subset.txt"))
    _write_cached(str(tmp_path / "TAB2010_TAB2020_ST10.zip"), buf.getvalue(), bw.REL_URL.format(state="10"), "application/zip")
    for y in (2010, 2020):
        _write_cached(str(tmp_path / f"dec{y}_pl_blocks_10.csv"), _read(f"dec{y}_pl_blocks_10_subset.csv"), "fixture", "text/csv")
    monkeypatch.setenv("CENSUS_BLOCK_DIR", str(tmp_path))
    monkeypatch.setenv("CENSUS_BLOCK_OFFLINE", "1")
    return tmp_path


def _toy_rel():
    # 2010 tract A (blocks a1, a2) -> 2020 tracts X, Y; block a2 is split 3:1 by land.
    return pd.DataFrame({
        "block_2010": ["A0001", "A0002", "A0002", "B0001"],
        "block_2020": ["X0001", "X0002", "Y0001", "Y0002"],
        "tract_2010": ["A", "A", "A", "B"],
        "tract_2020": ["X", "X", "Y", "Y"],
        "arealand_int": [100.0, 300.0, 100.0, 50.0],
        "areawater_int": [0.0, 0.0, 0.0, 0.0],
    })


def test_toy_population_weights_split_blocks_by_land():
    counts = pd.DataFrame({"block_geoid": ["A0001", "A0002", "B0001"], "population": [10, 40, 0], "housing_units": [5, 5, 0]})
    cw = bw.block_tract_weights(_toy_rel(), counts, weight="population")
    w = cw.set_index(["source_geoid", "target_geoid"])["weight"]
    # mass A->X = 10 + 40*0.75 = 40, A->Y = 10; total 50
    assert w[("A", "X")] == pytest.approx(0.8)
    assert w[("A", "Y")] == pytest.approx(0.2)
    assert w[("B", "Y")] == pytest.approx(1.0)  # no population: land-area fallback
    assert cw.attrs["fallbacks"]["no_count_land_area"] == 1
    hu = bw.block_tract_weights(_toy_rel(), counts, weight="housing_units").set_index(["source_geoid", "target_geoid"])["weight"]
    assert hu[("A", "X")] == pytest.approx((5 + 5 * 0.75) / 10)
    land = bw.block_tract_weights(_toy_rel(), None, weight="land_area").set_index(["source_geoid", "target_geoid"])["weight"]
    assert land[("A", "X")] == pytest.approx(400 / 500)


def test_reverse_direction_uses_target_blocks():
    counts20 = pd.DataFrame({"block_geoid": ["X0001", "X0002", "Y0001", "Y0002"], "population": [1, 1, 2, 6], "housing_units": [1, 1, 1, 1]})
    cw = bw.block_tract_weights(_toy_rel(), counts20, direction="2020_to_2010", weight="population")
    w = cw.set_index(["source_geoid", "target_geoid"])["weight"]
    assert w[("X", "A")] == pytest.approx(1.0)
    assert w[("Y", "A")] == pytest.approx(2 / 8)
    assert w[("Y", "B")] == pytest.approx(6 / 8)


def test_fixture_weights_sum_to_one_and_differ_from_land(rel, counts10):
    pop = bw.block_tract_weights(rel, counts10, weight="population")
    land = bw.block_tract_weights(rel, None, weight="land_area")
    for cw in (pop, land, bw.block_tract_weights(rel, counts10, weight="housing_units")):
        sums = cw.groupby("source_geoid")["weight"].sum()
        assert np.allclose(sums.values, 1.0, atol=1e-12)
        assert (cw["weight"] > 0).all()
    diff = bw.weight_differences(pop, land).set_index("source_geoid")
    assert diff.loc["10001041100", "tvd"] > 0.5  # most-changed Delaware tract (live: 0.766)
    assert set(pop.loc[pop.source_geoid == "10003014300", "target_geoid"]) == {"10003014301", "10003014302"}


def test_land_weights_match_tract_relationship_file(rel):
    land = bw.block_tract_weights(rel, None, weight="land_area")
    national = gc.tract_crosswalk()
    for src in FULL_SOURCES:
        a = land[land.source_geoid == src].set_index("target_geoid")["weight"]
        b = national[national.source_geoid == src].set_index("target_geoid")["weight"]
        pd.testing.assert_series_equal(a.sort_index(), b.sort_index(), check_names=False, atol=1e-6)


def test_validate_weights_conserves_county_totals(rel, counts10, counts20):
    tracts = pd.Series([1000.0, 2000.0], index=FULL_SOURCES)
    counties = pd.Series([1000.0, 2000.0], index=["10001", "10003"])
    rep = bw.validate_weights(rel, counts10, counts20, tracts, counties)
    assert rep["population_max_sum_error"] < 1e-12
    for name in ("population", "land_area"):
        assert rep["county_totals"][name]["max_abs_diff"] == pytest.approx(0, abs=1e-6)
    assert set(rep["decennial_2010_to_2020_tract_error"]) == {"population", "land_area"}


def test_weight_for_variable():
    assert bw.weight_for_variable("B25001_001E") == "housing_units"
    assert bw.weight_for_variable("B11001_001E") == "housing_units"
    assert bw.weight_for_variable("B19001_002E") == "housing_units"
    assert bw.weight_for_variable("B01003_001E") == "population"
    assert bw.weight_for_variable("B17001_002E") == "population"


def test_cached_loaders_and_tract_crosswalk(block_cache):
    rel = bw.load_block_relationships("10")
    assert rel["block_2010"].str.len().eq(15).all()
    c = bw.load_block_counts("10", 2010)
    assert c["block_geoid"].str.len().eq(15).all()
    cw = gc.tract_crosswalk(weight="population", states=["10"])
    assert cw.attrs["weight_type"] == "population"
    assert cw.attrs["fallbacks"]["block_data_unavailable"] == []
    assert list(cw.columns) == ["source_geoid", "target_geoid", "weight", "part_area"]
    part = cw[cw.source_geoid.isin(FULL_SOURCES)]
    assert np.allclose(part.groupby("source_geoid")["weight"].sum(), 1.0)


def test_tampered_cache_is_refetched_not_trusted(block_cache):
    with open(block_cache / "dec2010_pl_blocks_10.csv", "ab") as f:
        f.write(b"999999999999999,1,1\n")
    with pytest.raises(bw.BlockDataError, match="not cached"):
        bw.load_block_counts("10", 2010)


def test_fallback_to_land_area_with_note(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_BLOCK_DIR", str(tmp_path))
    monkeypatch.setenv("CENSUS_BLOCK_OFFLINE", "1")
    cw = gc.tract_crosswalk(weight="housing_units", states=["10"])
    assert cw.attrs["weight_type"] == "land_area"
    assert cw.attrs["requested_weight"] == "housing_units"
    assert cw.attrs["fallbacks"]["block_data_unavailable"] == ["10"]
    assert any("block data unavailable" in n for n in cw.attrs["notes"])
    assert cw["source_geoid"].str.startswith("10").all()
    with pytest.raises(ValueError, match="block data unavailable"):
        gc.tract_crosswalk(weight="population", states=["10"], block_fallback=False)


def test_harmonize_panel_auto_splits_population_and_housing(block_cache):
    meta = {
        "B01003_001E": {"label": "Estimate!!Total"},
        "B25001_001E": {"label": "Estimate!!Total"},
    }
    panel = pd.DataFrame({
        "GEOID": ["10003014300", "10003014301", "10003014302"],
        "Year": [2019, 2020, 2020],
        "B01003_001E": [3000.0, 1600.0, 1500.0],
        "B01003_001M": [100.0, 80.0, 80.0],
        "B25001_001E": [1200.0, 700.0, 600.0],
        "B25001_001M": [40.0, 30.0, 30.0],
    })
    out, rec = gc.harmonize_panel(panel, 2020, metadata=meta, return_record=True)
    assert rec["weight_type"] == "auto"
    assert rec["weights_by_variable"]["housing_units"] == ["B25001_001E", "B25001_001M"]
    assert rec["weight_records"]["population"]["weight_type"] == "population"
    rows = out[out.Year == 2019].set_index("GEOID")
    assert rows["B01003_001E"].sum() == pytest.approx(3000.0)
    assert rows["B25001_001E"].sum() == pytest.approx(1200.0)
    pop = bw.block_tract_weights(bw.load_block_relationships("10"), bw.load_block_counts("10", 2010), weight="population")
    w = pop[pop.source_geoid == "10003014300"].set_index("target_geoid")["weight"]
    assert rows.loc["10003014301", "B01003_001E"] == pytest.approx(3000.0 * w["10003014301"])
    assert list(out.columns) == list(panel.columns)


@pytest.mark.skipif(os.environ.get("CENSUS_LIVE_TESTS") != "1", reason="set CENSUS_LIVE_TESTS=1 to run live tests")
def test_live_delaware_validation():
    rep = bw.validate_state("10", acs_year=2019)
    assert rep["population_max_sum_error"] < 1e-9 and rep["housing_units_max_sum_error"] < 1e-9
    ct = rep["county_totals"]["population"]
    assert ct["state_total_apportioned"] == pytest.approx(ct["state_total_published"], rel=1e-9)
    # A handful of tiny 2010/2020 county-boundary corrections move < 10 people.
    assert ct["max_abs_diff"] < 10
    err = rep["decennial_2010_to_2020_tract_error"]
    assert err["population"]["mean_abs_error"] < err["land_area"]["mean_abs_error"]
