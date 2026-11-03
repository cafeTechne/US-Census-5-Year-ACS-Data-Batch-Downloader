# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for census.geo_crosswalk (2010/2020 tract crosswalks)."""

import gzip
import hashlib
import json
import math
import os

import numpy as np
import pandas as pd
import pytest

from census import geo_crosswalk as gc
from census.variance import METHOD_APPORTIONED

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "geo_crosswalk")
SAMPLE = os.path.join(FIXTURES, "tract_2010_2020_sample.csv")
CT_SAMPLE = os.path.join(FIXTURES, "ct_tract_2020_2022_sample.csv")

# Autauga County, AL: 2010 tract 0205.00 was split into 0205.01-.03 (real rows).
SPLIT_2010 = "01001020500"
SPLIT_2020 = {"01001020501": 6203654, "01001020502": 2097390, "01001020503": 3107823}
UNCHANGED = "09001010101"  # Fairfield County, CT 0101.01, identical in both vintages

COUNT_META = {
    "B01003_001E": {"label": "Estimate!!Total", "concept": "TOTAL POPULATION"},
    "B19025_001E": {"label": "Estimate!!Aggregate household income in the past 12 months"},
}


@pytest.fixture(scope="module")
def sample_rel():
    return gc.load_tract_relationships(SAMPLE)


@pytest.fixture(scope="module")
def cw(sample_rel):
    return gc.tract_crosswalk("2010_to_2020", relationships=sample_rel)


def _frame(rows):
    return pd.DataFrame(rows, columns=["GEOID", "B01003_001E", "B01003_001M"])


# ---------------------------------------------------------------------------
# Bundled data
# ---------------------------------------------------------------------------


def test_bundled_extract_matches_sidecar():
    with open(gc.RELATIONSHIP_SIDECAR, encoding="utf-8") as f:
        meta = json.load(f)
    with gzip.open(gc.RELATIONSHIP_FILE, "rb") as f:
        raw = f.read()
    assert hashlib.sha256(raw).hexdigest() == meta["csv_sha256"]
    assert meta["source_url"].endswith("tab20_tract20_tract10_natl.txt")
    assert os.path.getsize(gc.RELATIONSHIP_FILE) < 10 * 1024 * 1024
    rel = gc.load_tract_relationships()
    assert len(rel) == meta["rows"]
    assert rel["geoid_2010"].str.len().eq(11).all() and rel["geoid_2020"].str.len().eq(11).all()


def test_gzip_header_is_deterministic():
    with open(gc.RELATIONSHIP_FILE, "rb") as f:
        header = f.read(10)
    assert header[4:8] == b"\x00\x00\x00\x00"  # mtime = 0
    assert header[3] & 0x08 == 0  # no FNAME field


@pytest.mark.parametrize("direction", gc.DIRECTIONS)
def test_full_crosswalk_weights_sum_to_one(direction):
    cw = gc.tract_crosswalk(direction)
    sums = cw.groupby("source_geoid")["weight"].sum()
    assert np.allclose(sums.values, 1.0, atol=1e-9)
    assert (cw["weight"] > 0).all() and (cw["weight"] <= 1 + 1e-12).all()
    assert cw.attrs["fallbacks"]["total_area"] > 0  # water-only tracts exist
    assert cw.attrs["fallbacks"]["equal_split"] == 0


def test_real_split_tract_in_full_crosswalk():
    cw = gc.tract_crosswalk()
    part = cw[cw["source_geoid"] == SPLIT_2010].set_index("target_geoid")["weight"]
    total = sum(SPLIT_2020.values())
    assert set(part.index) == set(SPLIT_2020)
    for geoid, land in SPLIT_2020.items():
        assert part[geoid] == pytest.approx(land / total)


def test_ct_recode_bundled():
    table = gc.load_ct_tract_recode()
    assert table["geoid_2020"].str.startswith("09").all()
    assert table["geoid_2022"].str[2:5].astype(int).between(110, 190).all()
    assert (table["geoid_2020"].str[5:] == table["geoid_2022"].str[5:]).all()
    assert table["geoid_2020"].is_unique and table["geoid_2022"].is_unique


def test_acs_renumbering_targets_are_not_census_codes():
    rel = gc.load_tract_relationships()
    census2010 = set(rel["geoid_2010"])
    for old, new in gc.ACS_2010_TRACT_RENUMBERING.items():
        assert old in census2010
        assert new not in census2010
    for old in gc.ACS_2010_COUNTY_RECODES:
        assert rel["geoid_2010"].str.startswith(old).any()


# ---------------------------------------------------------------------------
# Crosswalk construction
# ---------------------------------------------------------------------------


def test_sample_weights_sum_to_one(cw):
    assert np.allclose(cw.groupby("source_geoid")["weight"].sum(), 1.0)
    assert list(cw.columns[:3]) == ["source_geoid", "target_geoid", "weight"]


def test_unchanged_tract_weight_one(cw):
    rows = cw[cw["source_geoid"] == UNCHANGED]
    assert len(rows) == 1
    assert rows["target_geoid"].iloc[0] == UNCHANGED
    assert rows["weight"].iloc[0] == 1.0


def test_water_only_tract_falls_back_to_total_area(cw):
    rows = cw[cw["source_geoid"] == "01003990000"]
    assert rows["weight"].tolist() == [1.0]
    assert cw.attrs["fallbacks"]["total_area"] == 1


def test_equal_split_when_no_area():
    rel = pd.DataFrame(
        {
            "geoid_2010": ["01001000100", "01001000100"],
            "geoid_2020": ["01001000101", "01001000102"],
            "arealand_part": [0, 0],
            "areawater_part": [0, 0],
        }
    )
    cw = gc.tract_crosswalk(relationships=rel)
    assert cw["weight"].tolist() == [0.5, 0.5]
    assert cw.attrs["fallbacks"]["equal_split"] == 1


def test_reverse_direction(sample_rel):
    cw = gc.tract_crosswalk("2020_to_2010", relationships=sample_rel)
    rows = cw[cw["source_geoid"] == "01001020501"]
    assert rows[["target_geoid", "weight"]].values.tolist() == [[SPLIT_2010, 1.0]]


def test_invalid_arguments(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        gc.tract_crosswalk("2000_to_2010")
    # Round 9: population weights come from block data; offline with no cache -> error when no fallback.
    monkeypatch.setenv("CENSUS_BLOCK_DIR", str(tmp_path))
    monkeypatch.setenv("CENSUS_BLOCK_OFFLINE", "1")
    with pytest.raises(ValueError, match="block data unavailable"):
        gc.tract_crosswalk(weight="population", states=["10"], block_fallback=False)
    with pytest.raises(ValueError):
        gc.tract_crosswalk(weight="housing")


# ---------------------------------------------------------------------------
# User weight files
# ---------------------------------------------------------------------------


def test_load_weight_file_pads_and_validates(tmp_path):
    path = tmp_path / "nhgis.csv"
    path.write_text(
        "tr2010ge,tr2020ge,wt_pop\n"
        "1001020500,1001020501,0.6\n"
        "1001020500,1001020502,0.4\n"
        "9001010101,9001010101,1\n"
    )
    cw = gc.load_weight_file(
        str(path), columns={"tr2010ge": "source_geoid", "tr2020ge": "target_geoid", "wt_pop": "weight"}
    )
    assert set(cw["source_geoid"]) == {SPLIT_2010, UNCHANGED}
    assert cw["target_geoid"].str.len().eq(11).all()
    assert cw.groupby("source_geoid")["weight"].sum().tolist() == [1.0, 1.0]


def test_load_weight_file_rejects_bad_sums(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("source_geoid,target_geoid,weight\n01001020500,01001020501,0.6\n01001020500,01001020502,0.3\n")
    with pytest.raises(ValueError, match="do not sum to 1"):
        gc.load_weight_file(str(path))
    cw = gc.load_weight_file(str(path), renormalize=True)
    assert cw["weight"].sum() == pytest.approx(1.0)
    path.write_text("source_geoid,target_geoid,weight\n01001020500,01001020501,1.5\n")
    with pytest.raises(ValueError, match="outside"):
        gc.load_weight_file(str(path))
    path.write_text("source,target,weight\n1,2,1\n")
    with pytest.raises(ValueError, match="missing column"):
        gc.load_weight_file(str(path))


def test_population_weights_via_tract_crosswalk(tmp_path):
    path = tmp_path / "pop.csv"
    path.write_text("source_geoid,target_geoid,weight\n01001020500,01001020501,0.5\n01001020500,01001020502,0.5\n")
    cw = gc.tract_crosswalk(weight="population", weights_file=str(path))
    assert cw.attrs["weight_type"] == "population"
    out = gc.apportion(_frame([[SPLIT_2010, 100, 20]]), cw, metadata=COUNT_META, min_coverage=0)
    assert out["B01003_001E"].tolist() == [50.0, 50.0]
    assert "population" in out.attrs["notes"][0]


# ---------------------------------------------------------------------------
# Apportionment
# ---------------------------------------------------------------------------


def test_split_tract_apportions_to_successors(cw):
    df = _frame([[SPLIT_2010, 1000.0, 100.0]])
    out = gc.apportion(df, cw, metadata=COUNT_META).set_index("GEOID")
    total = sum(SPLIT_2020.values())
    assert set(out.index) == set(SPLIT_2020)
    for geoid, land in SPLIT_2020.items():
        w = land / total
        assert out.loc[geoid, "B01003_001E"] == pytest.approx(1000 * w)
        assert out.loc[geoid, "B01003_001M"] == pytest.approx(100 * w)
    assert out["B01003_001E"].sum() == pytest.approx(1000.0)
    assert out.attrs["method"] == METHOD_APPORTIONED == "apportioned"
    assert "land_area" in out.attrs["notes"][0]
    assert any("independent" in n for n in out.attrs["notes"])


def test_moe_propagation_merge(cw):
    # 2020 tract 01001020100 = most of 2010 0201.00 + a sliver of 2010 0202.00.
    df = _frame([["01001020100", 500.0, 30.0], ["01001020200", 400.0, 40.0]])
    out = gc.apportion(df, cw, metadata=COUNT_META, min_coverage=0).set_index("GEOID")
    w1 = 9820448 / (9820448 + 6823)
    w2 = 4856 / (4856 + 3320818)
    assert out.loc["01001020100", "B01003_001E"] == pytest.approx(w1 * 500 + w2 * 400)
    assert out.loc["01001020100", "B01003_001M"] == pytest.approx(math.hypot(w1 * 30, w2 * 40))


def test_unchanged_tract_values_identical(cw):
    out = gc.apportion(_frame([[UNCHANGED, 1234.0, 56.0]]), cw, metadata=COUNT_META)
    assert out.iloc[0].tolist() == [UNCHANGED, 1234.0, 56.0]


def test_zero_estimate_rule(cw):
    df = _frame([["01001020100", 0.0, 30.0], ["01001020200", 0.0, 40.0]])
    all_ = gc.apportion(df, cw, metadata=COUNT_META, min_coverage=0).set_index("GEOID")
    largest = gc.apportion(df, cw, metadata=COUNT_META, min_coverage=0, zero_estimate_rule="largest").set_index("GEOID")
    w1 = 9820448 / (9820448 + 6823)
    w2 = 4856 / (4856 + 3320818)
    assert all_.loc["01001020100", "B01003_001M"] == pytest.approx(math.hypot(w1 * 30, w2 * 40))
    assert largest.loc["01001020100", "B01003_001M"] == pytest.approx(max(w1 * 30, w2 * 40))


def test_sentinels_propagate_as_undefined(cw):
    df = _frame([["01001020100", -666666666, -222222222], ["01001020200", 400.0, -555555555]])
    out = gc.apportion(df, cw, metadata=COUNT_META, min_coverage=0).set_index("GEOID")
    assert math.isnan(out.loc["01001020100", "B01003_001E"])
    assert math.isnan(out.loc["01001020100", "B01003_001M"])
    # Controlled MOE (*****) counts as 0.
    assert out.loc["01001020200", "B01003_001M"] == 0.0


def test_incomplete_targets_dropped(cw):
    # Only one of the two sources of 2020 tract 01001020100 is present.
    df = _frame([["01001020200", 400.0, 40.0]])
    out = gc.apportion(df, cw, metadata=COUNT_META)
    assert "01001020100" not in set(out["GEOID"])
    assert out.attrs["incomplete_targets"] == ["01001020100"]
    kept = gc.apportion(df, cw, metadata=COUNT_META, min_coverage=0)
    assert "01001020100" in set(kept["GEOID"])


def test_geoid_zero_padding(cw):
    df = _frame([[1001020500, 1000.0, 100.0]])  # leading zero lost
    out = gc.apportion(df, cw, metadata=COUNT_META)
    assert set(out["GEOID"]) == set(SPLIT_2020)
    assert gc.normalize_tract_geoid("1400000US01001020500") == SPLIT_2010
    assert gc.normalize_tract_geoid(1001020500.0) == SPLIT_2010
    with pytest.raises(ValueError):
        gc.normalize_tract_geoid("ABC")


def test_unmatched_geoids_reported(cw):
    out = gc.apportion(_frame([[SPLIT_2010, 1.0, 1.0], ["99999999999", 5.0, 1.0]]), cw, metadata=COUNT_META)
    assert out.attrs["unmatched_geoids"] == ["99999999999"]


# ---------------------------------------------------------------------------
# Non-additive refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "col,meta",
    [
        ("B19013_001E", {}),  # median household income (known table)
        ("B25010_001E", {}),  # average household size
        ("DP03_0128PE", {}),  # percent
        ("S1701_C03_001E", {"S1701_C03_001E": {"label": "Estimate!!Percent below poverty level!!Population"}}),
        ("B99999_001E", {"B99999_001E": {"label": "Estimate!!Per capita income"}}),
        ("B23025_099E", {"B23025_099E": {"label": "Estimate!!Unemployment rate"}}),
    ],
)
def test_refuses_non_additive(cw, col, meta):
    df = pd.DataFrame({"GEOID": [SPLIT_2010], col: [50.0]})
    with pytest.raises(ValueError, match="re-derive"):
        gc.apportion(df, cw, metadata=meta)


def test_refuses_named_non_additive_columns(cw):
    df = pd.DataFrame({"GEOID": [SPLIT_2010], "median_income": [5.0], "median_income_moe": [1.0]})
    with pytest.raises(ValueError, match="non-additive"):
        gc.apportion(df, cw, estimate_cols=["median_income"], assume_additive=False)


def test_assume_additive_never_overrides_known_medians(cw):
    df = pd.DataFrame({"GEOID": [SPLIT_2010], "B19013_001E": [50.0], "B19013_001M": [5.0]})
    with pytest.raises(ValueError):
        gc.apportion(df, cw, assume_additive=True)


def test_drop_non_additive(cw):
    df = pd.DataFrame(
        {"GEOID": [SPLIT_2010], "B01003_001E": [100.0], "B01003_001M": [10.0],
         "B19013_001E": [50000.0], "B19013_001M": [5000.0]}
    )
    out = gc.apportion(df, cw, metadata=COUNT_META, non_additive="drop")
    assert "B19013_001E" not in out.columns
    assert "B19013_001E" in out.attrs["dropped_columns"]


def test_unlabelled_columns_need_metadata_or_assumption(cw):
    df = pd.DataFrame({"GEOID": [SPLIT_2010], "B99998_001E": [100.0], "B99998_001M": [10.0]})
    with pytest.raises(ValueError, match="verify"):
        gc.apportion(df, cw, metadata={})
    out = gc.apportion(df, cw, metadata={}, assume_additive=True)
    assert out["B99998_001E"].sum() == pytest.approx(100.0)


def test_offline_archive_labels_used_by_default(cw):
    df = _frame([[SPLIT_2010, 1000.0, 100.0]])
    out = gc.apportion(df, cw)  # B01003_001E label from the metadata archive
    assert out["B01003_001E"].sum() == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Panel harmonization
# ---------------------------------------------------------------------------


def _panel():
    return pd.DataFrame(
        {
            "GEOID": [SPLIT_2010, UNCHANGED, "01001020501", "01001020502", "01001020503", UNCHANGED],
            "Year": [2019, 2019, 2022, 2022, 2022, 2021],
            "NAME": ["a", "b", "c", "d", "e", "f"],
            "B01003_001E": [1000.0, 300.0, 600.0, 200.0, 250.0, 310.0],
            "B01003_001M": [100.0, 30.0, 60.0, 20.0, 25.0, 31.0],
            "B01003_001_SE": [100 / 1.645, 30 / 1.645, 60 / 1.645, 20 / 1.645, 25 / 1.645, 31 / 1.645],
            "B01003_001_CV": [1.0] * 6,
            "B01003_001_RELIABILITY": ["High"] * 6,
        }
    )


def test_harmonize_panel_to_2020(sample_rel):
    cw = gc.tract_crosswalk(relationships=sample_rel)
    out, rec = gc.harmonize_panel(_panel(), 2020, crosswalk=cw, metadata=COUNT_META, return_record=True)
    y2019 = out[out["Year"] == 2019].set_index("GEOID")
    assert set(y2019.index) == set(SPLIT_2020) | {"09190010101"}
    assert y2019["B01003_001E"].drop("09190010101").sum() == pytest.approx(1000.0)
    se = y2019.loc["01001020501", "B01003_001_SE"]
    assert se == pytest.approx(y2019.loc["01001020501", "B01003_001M"] / 1.645)
    assert y2019.loc["01001020501", "B01003_001_CV"] == pytest.approx(se / y2019.loc["01001020501", "B01003_001E"] * 100)
    assert y2019.loc["01001020501", "NAME"] == ""
    # Native 2022 rows untouched; Connecticut 2021 row recoded to planning-region GEOID.
    native = out[out["Year"] == 2022].set_index("GEOID")["B01003_001E"].to_dict()
    assert native == {"01001020501": 600.0, "01001020502": 200.0, "01001020503": 250.0}
    assert out[(out["Year"] == 2021)]["GEOID"].tolist() == ["09190010101"]
    assert rec["years_apportioned"] == [2019]
    assert rec["years_native"] == [2021, 2022]
    assert rec["connecticut_coding"] == "2022"
    assert rec["method"] == "apportioned"
    assert out.attrs["harmonization"] is rec
    assert not out.duplicated(["GEOID", "Year"]).any()


def test_harmonize_panel_to_2010(sample_rel):
    cw = gc.tract_crosswalk("2020_to_2010", relationships=sample_rel)
    panel = _panel()
    panel.loc[panel["Year"] == 2021, "GEOID"] = "09190010101"  # pretend 2022+ coding
    panel.loc[panel["Year"] == 2021, "Year"] = 2023
    out, rec = gc.harmonize_panel(panel, 2010, crosswalk=cw, metadata=COUNT_META, return_record=True)
    y2022 = out[out["Year"] == 2022].set_index("GEOID")
    assert y2022.loc[SPLIT_2010, "B01003_001E"] == pytest.approx(1050.0)
    assert y2022.loc[SPLIT_2010, "B01003_001M"] == pytest.approx(math.sqrt(60**2 + 20**2 + 25**2))
    assert out[out["Year"] == 2023]["GEOID"].tolist() == [UNCHANGED]
    assert rec["geoid_recodes"]["ct_planning_region_to_2020_county"] == 1


def test_harmonize_panel_recodes_renumbered_2010_tracts(sample_rel):
    cw = gc.tract_crosswalk(relationships=sample_rel)
    # ACS 2011-2019 code for Madison County, NY tract 9401.02 is 0301.02.
    panel = pd.DataFrame({"GEOID": ["36053030102"], "Year": [2015], "B01003_001E": [10.0], "B01003_001M": [2.0]})
    out, rec = gc.harmonize_panel(panel, 2020, crosswalk=cw, metadata=COUNT_META, return_record=True)
    assert out["GEOID"].tolist() == ["36053030102"]
    assert out["B01003_001E"].tolist() == [10.0]
    assert rec["geoid_recodes"]["acs_2010_codes_to_census_2010"] == 1
    assert gc.acs_to_census2010_geoid("46102940500") == "46113940500"
    assert gc.census2010_to_acs_geoid("46113940500") == "46102940500"


def test_harmonize_panel_long_format(sample_rel):
    cw = gc.tract_crosswalk(relationships=sample_rel)
    long = pd.DataFrame(
        {"GEOID": [SPLIT_2010, "01001020501"], "Year": [2018, 2020], "variable": ["B01003_001E"] * 2,
         "estimate": [1000.0, 600.0], "moe": [100.0, 60.0]}
    )
    out = gc.harmonize_panel(long, 2020, crosswalk=cw, metadata=COUNT_META)
    app = out[out["Year"] == 2018]
    assert app["estimate"].sum() == pytest.approx(1000.0)
    assert (app["method"] == "apportioned").all()
    assert {"se", "cv_percent", "notes"} <= set(out.columns)


def test_harmonize_panel_refuses_non_additive(sample_rel):
    cw = gc.tract_crosswalk(relationships=sample_rel)
    panel = _panel()
    panel["B19013_001E"] = 50000.0
    with pytest.raises(ValueError, match="re-derive"):
        gc.harmonize_panel(panel, 2020, crosswalk=cw, metadata=COUNT_META)
    out = gc.harmonize_panel(panel, 2020, crosswalk=cw, metadata=COUNT_META, non_additive="drop")
    assert out.loc[out["Year"] == 2019, "B19013_001E"].isna().all()
    assert (out.loc[out["Year"] == 2022, "B19013_001E"] == 50000.0).all()


def test_tract_vintage():
    assert gc.tract_vintage(2019) == 2010
    assert gc.tract_vintage(2020) == 2020
    assert gc.tract_vintage(2024) == 2020
    with pytest.raises(ValueError):
        gc.tract_vintage(2009)


def test_ct_recode_both_ways():
    out = gc.recode_ct_tracts(["09001010101", "01001020500"], to="2022", path=CT_SAMPLE)
    assert out.tolist() == ["09190010101", "01001020500"]
    back = gc.recode_ct_tracts(["09190010101"], to="2020", path=CT_SAMPLE)
    assert back.tolist() == ["09001010101"]
