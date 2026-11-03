# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block-group (summary level 150) requests, entities, GEOIDs and planning (offline)."""

import os
from unittest import mock

import pytest

from census import api
from census.pipeline import (
    BLOCK_GROUP_COUNTY_LIST_YEAR,
    ResearchOptions,
    build_entities,
    plan_extraction,
    prepare_frame,
    probe_block_group_availability,
)

RAW = [
    ["NAME", "B01003_001E", "B01003_001M", "GEO_ID", "state", "county", "tract", "block group"],
    ["Block Group 2, Census Tract 412, Kent County, Delaware", "2595", "300", "1500000US100010412002",
     "10", "001", "041200", "2"],
]


def test_geo_params_and_levels():
    assert api.GEO_LEVELS["block group"] == ("block group", "150")
    p = api.build_geo_params("block_group", state_code="10", county_code="001", year=2019)
    assert p == {"for": "block group:*", "in": "state:10 county:001"}
    with pytest.raises(ValueError, match="one state"):
        api.build_geo_params("block group", state_code="*")
    with pytest.raises(ValueError, match="2013"):
        api.build_geo_params("block group", state_code="10", county_code="001", year=2012)
    client = api.CensusAPIClient(api_key="")
    url, params = client.build_request(2019, "10", "B01003", geo_level="block group", county_code="003")
    assert url.endswith("/2019/acs/acs5") and params["for"] == "block group:*"


def test_entities_one_per_county():
    ents = build_entities("block group", "10", ["001", "3"])
    assert [e.county_code for e in ents] == ["001", "003"]
    assert {e.group for e in ents} == {"Delaware_Block_Groups"}
    client = mock.Mock()
    client.list_geographies.return_value = [{"county": "001"}, {"county": "003"}, {"county": "005"}]
    ents = build_entities("bg", "10", client=client)
    assert len(ents) == 3 and all(e.geo_level == "block group" for e in ents)
    assert client.list_geographies.call_args[0][:2] == (BLOCK_GROUP_COUNTY_LIST_YEAR, "county")
    client.list_geographies.side_effect = OSError("offline")
    ents = build_entities("block group", "10", client=client)
    assert len(ents) == 1 and ents[0].county_code == "*"
    with pytest.raises(ValueError):
        build_entities("block group", None)


def test_nationwide_block_groups_per_county():
    client = mock.Mock()
    client.list_geographies.return_value = [{"county": "001"}]
    ents = build_entities("block group", "all", client=client)
    assert len(ents) == 51 and len({e.group for e in ents}) == 1


def test_prepare_frame_builds_12_digit_geoid():
    df = prepare_frame(RAW, 2019, ResearchOptions(), {}, "block group")
    assert df["GEOID"].tolist() == ["100010412002"]
    no_geo_id = [[c for c in r if not str(c).startswith("1500000US") and c != "GEO_ID"] for r in RAW]
    df = prepare_frame(no_geo_id, 2019, ResearchOptions(), {}, "block group")
    assert df["GEOID"].tolist() == ["100010412002"]


def _probe_client(values):
    client = mock.Mock()
    client.build_request.return_value = ("u", {})
    header = ["NAME", "B05006_001E", "B05006_001M", "state", "county", "tract", "block group"]
    client._get_json.side_effect = [[header, ["x", v, None, "10", "001", "041200", "2"]] if v != "fail" else None
                                    for v in values]
    return client


def test_probe_block_group_availability():
    ent = build_entities("block group", "10", ["001"])[0]
    rep = probe_block_group_availability(_probe_client([None, "12", "fail"]), "B05006", [2019, 2020, 2021], ent)
    assert rep["unpublished_years"] == [2019]
    assert rep["unchecked_years"] == [2021]


def test_plan_rejects_subject_tables_and_early_years():
    ents = build_entities("block group", "10", ["001"])
    client = mock.Mock()
    out = plan_extraction("S0101", ents, [2019], client=client)
    assert not out["success"] and "detailed tables" in out["error"]
    out = plan_extraction("B01003", ents, [2012, 2019], client=client)
    assert not out["success"] and "2013" in out["error"]
    client.assert_not_called()


@pytest.mark.skipif(os.environ.get("CENSUS_LIVE_TESTS") != "1", reason="set CENSUS_LIVE_TESTS=1 to run live tests")
def test_live_block_groups_delaware():
    client = api.CensusAPIClient()
    data = client.fetch(2019, "10", "B01003", geo_level="block group", county_code="001")
    df = prepare_frame(data, 2019, ResearchOptions(), {}, "block group")
    assert df["GEOID"].str.len().eq(12).all() and len(df) > 50
    ents = build_entities("block group", "10", client=client)
    assert len(ents) == 3
    rep = probe_block_group_availability(client, "B05006", [2019], ents[0])
    assert rep["unpublished_years"] == [2019]
