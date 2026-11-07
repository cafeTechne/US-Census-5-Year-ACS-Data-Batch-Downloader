# SPDX-License-Identifier: AGPL-3.0-or-later
"""ZCTA and place 2010 <-> 2020 crosswalks from block assignments (Round 10 F2). Offline."""

import io
import zipfile

import pandas as pd
import pytest

from census import block_weights as bw
from census import geo_crosswalk as gc

# 15-digit block GEOIDs in DC (11) and Maryland (24).
B = {k: v for k, v in {
    "a1": "110010001001000", "a2": "110010001001001", "a3": "110010001001002",
    "x1": "110010001011000", "x2": "110010001011001", "x3": "110010001011002", "x4": "110010001011003",
    "m1": "240330001001000", "y1": "240330001011000",
}.items()}


def _rel():
    # 2010 block a2 is split 3:1 by land between 2020 blocks x2 and x3.
    return pd.DataFrame({
        "block_2010": [B["a1"], B["a2"], B["a2"], B["a3"], B["m1"]],
        "block_2020": [B["x1"], B["x2"], B["x3"], B["x4"], B["y1"]],
        "tract_2010": [b[:11] for b in (B["a1"], B["a2"], B["a2"], B["a3"], B["m1"])],
        "tract_2020": [b[:11] for b in (B["x1"], B["x2"], B["x3"], B["x4"], B["y1"])],
        "arealand_int": [100.0, 300.0, 100.0, 50.0, 80.0],
        "areawater_int": [0.0, 0.0, 0.0, 0.0, 0.0],
    })


COUNTS10 = pd.DataFrame({"block_geoid": [B["a1"], B["a2"], B["a3"], B["m1"]],
                         "population": [10, 40, 5, 20], "housing_units": [4, 10, 2, 8]})
# 2010: ZCTA 20001 = a1, a2; 20002 = a3; Maryland block m1 in ZCTA 20012 (straddles DC/MD).
Z10 = pd.DataFrame({"block_geoid": [B["a1"], B["a2"], B["a3"], B["m1"]],
                    "area_geoid": ["20001", "20001", "20002", "20012"]})
# 2020: x1, x2 -> 20001; x3 -> 20012; x4 in no ZCTA (water); y1 -> 20012.
Z20 = pd.DataFrame({"block_geoid": [B["x1"], B["x2"], B["x3"], B["y1"]],
                    "area_geoid": ["20001", "20001", "20012", "20012"]})


def test_zcta_weights_from_blocks_and_unassigned_targets():
    cw = bw.block_area_weights(_rel(), COUNTS10, Z10, Z20, weight="population", geography="zcta")
    w = cw.set_index(["source_geoid", "target_geoid"])["weight"]
    # 20001 mass: a1 10 -> 20001; a2 40 * 0.75 = 30 -> 20001, 10 -> 20012. Total 50.
    assert w[("20001", "20001")] == pytest.approx(40 / 50)
    assert w[("20001", "20012")] == pytest.approx(10 / 50)
    # 20002's only block went to x4, in no 2020 ZCTA: no output row, mass reported.
    assert "20002" not in set(cw["source_geoid"])
    assert cw.attrs["unassigned_share"] == pytest.approx(5 / 75)
    assert w[("20012", "20012")] == pytest.approx(1.0)
    cov = cw.groupby("target_geoid")["target_coverage"].first()
    assert cov["20001"] == pytest.approx(1.0)
    assert any("outside every target" in n for n in cw.attrs["notes"])


def test_place_weights_sum_below_one_and_target_coverage():
    p10 = pd.DataFrame({"block_geoid": [B["a1"], B["a2"]], "area_geoid": ["1150000", "1150000"]})
    # 2020: the place also includes x4 (from 2010 block a3, in no 2010 place).
    p20 = pd.DataFrame({"block_geoid": [B["x1"], B["x2"], B["x4"]], "area_geoid": ["1150000"] * 3})
    cw = bw.block_area_weights(_rel(), COUNTS10, p10, p20, weight="population", geography="place")
    row = cw.set_index(["source_geoid", "target_geoid"]).loc[("1150000", "1150000")]
    assert row["weight"] == pytest.approx(40 / 50)  # 10 people (part of a2) left every 2020 place
    assert row["target_coverage"] == pytest.approx(40 / 45)  # 5 people came from no 2010 place
    land = bw.block_area_weights(_rel(), None, p10, p20, weight="land_area", geography="place")
    assert land["weight"].iloc[0] == pytest.approx(400 / 500)


def test_reverse_direction_uses_2020_assignment_as_source():
    c20 = pd.DataFrame({"block_geoid": [B["x1"], B["x2"], B["x3"], B["x4"], B["y1"]],
                        "population": [1, 3, 4, 0, 2], "housing_units": [1, 1, 1, 0, 1]})
    cw = bw.block_area_weights(_rel(), c20, Z10, Z20, direction="2020_to_2010", weight="population")
    w = cw.set_index(["source_geoid", "target_geoid"])["weight"]
    assert w[("20001", "20001")] == pytest.approx(1.0)
    assert w[("20012", "20001")] == pytest.approx(4 / 6)
    assert w[("20012", "20012")] == pytest.approx(2 / 6)


def test_parsers_for_census_assignment_formats():
    baf2020 = "BLOCKID|PLACEFP\n110010001011000|50000\n110010001011001|\n"
    baf2010 = "BLOCKID,PLACEFP\r\n240330001001000,04100\r\n"
    assert bw.parse_place_assignment(baf2020).to_dict("records") == [
        {"block_geoid": "110010001011000", "area_geoid": "1150000"}]
    assert bw.parse_place_assignment(baf2010)["area_geoid"].tolist() == ["2404100"]
    zcta = ["﻿OID_ZCTA5_20|GEOID_ZCTA5_20|NAMELSAD_ZCTA5_20|OID_TABBLOCK_20|GEOID_TABBLOCK_20",
            "1|20001|ZCTA5 20001|9|110010001011000", "||||110010001011003"]
    assert bw.parse_zcta520_blocks(zcta)["area_geoid"].tolist() == ["20001"]
    faces = pd.DataFrame({"STATEFP10": ["11"] * 4, "COUNTYFP10": ["001"] * 4, "TRACTCE10": ["000100"] * 4,
                          "BLOCKCE10": ["1000", "1000", "1001", None], "ZCTA5CE10": ["20001", "20001", "20002", "20003"]})
    z = bw.faces_to_zcta(faces)
    assert dict(zip(z["block_geoid"], z["area_geoid"])) == {B["a1"]: "20001", B["a2"]: "20002"}
    assert z.attrs["conflicts"] == 0
    with pytest.raises(bw.BlockDataError):
        bw.parse_place_assignment("GEOID,X\n1,2\n")


def test_cached_place_assignment_and_crosswalk_pool_states(tmp_path, monkeypatch):
    """area_weights pools DC and Maryland pieces (a ZCTA straddling both) from caches."""
    monkeypatch.setenv("CENSUS_BLOCK_DIR", str(tmp_path))
    monkeypatch.setenv("CENSUS_BLOCK_OFFLINE", "1")
    rel = _rel()
    monkeypatch.setattr(bw, "load_block_relationships", lambda st, session=None: rel)
    monkeypatch.setattr(bw, "load_block_counts", lambda st, year, **k: COUNTS10)
    for st in ("11", "24"):
        for year, frame in ((2010, Z10), (2020, Z20)):
            part = frame[frame["block_geoid"].str[:2] == st]
            bw._write_assignment_cache(str(tmp_path / f"zcta{510 if year == 2010 else 520}_blocks_{st}.csv"),
                                       part, "fixture")
    cw = gc.zcta_crosswalk(states=["11", "24"])
    assert list(cw.columns) == ["source_geoid", "target_geoid", "weight", "part_area", "target_coverage"]
    w = cw.set_index(["source_geoid", "target_geoid"])["weight"]
    assert w[("20012", "20012")] == pytest.approx(1.0)
    assert w[("20001", "20012")] == pytest.approx(0.2)
    assert "zcta520" in str(cw.attrs["source"]["assignment"][2020])
    # Offline with no cache: a clear error, not a download.
    with pytest.raises(bw.BlockDataError, match="CENSUS_BLOCK_OFFLINE"):
        bw.load_block_assignment("11", "place", 2020)
    # Apportion counts with the ZCTA crosswalk (non-tract GEOIDs kept as strings).
    df = pd.DataFrame({"GEOID": ["20001", "20012"], "B01003_001E": [50.0, 20.0], "B01003_001M": [5.0, 4.0]})
    out = gc.apportion(df, cw, min_coverage=0.0)
    got = out.set_index("GEOID")["B01003_001E"]
    assert got["20001"] == pytest.approx(40.0)
    assert got["20012"] == pytest.approx(30.0)


def test_place_assignment_from_baf_zip(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_BLOCK_DIR", str(tmp_path))
    monkeypatch.delenv("CENSUS_BLOCK_OFFLINE", raising=False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BlockAssign_ST11_DC_VTD.txt", "BLOCKID|DISTRICT\n")
        zf.writestr("BlockAssign_ST11_DC_INCPLACE_CDP.txt", "BLOCKID|PLACEFP\n110010001011000|50000\n")
    seen = []

    def fake_get(url, params=None, session=None, timeout=300.0):
        seen.append(url)
        return buf.getvalue()

    monkeypatch.setattr(bw, "_get", fake_get)
    df = bw.load_block_assignment("11", "place", 2020)
    assert df["area_geoid"].tolist() == ["1150000"]
    assert seen == ["https://www2.census.gov/geo/docs/maps-data/data/baf2020/BlockAssign_ST11_DC.zip"]
    bw.load_block_assignment("11", "place", 2020)  # cached: no second request
    assert len(seen) == 1
