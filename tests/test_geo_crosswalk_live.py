# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live checks of the bundled tract crosswalk against census.gov.

Skipped unless CENSUS_LIVE_TESTS=1. Downloads are parsed only.
"""

import hashlib
import json
import os

import pytest
import requests

from census import geo_crosswalk as gc

pytestmark = pytest.mark.skipif(
    os.environ.get("CENSUS_LIVE_TESTS") != "1", reason="set CENSUS_LIVE_TESTS=1 to run live tests"
)


def _api_rows(url, params):
    key = os.environ.get("CENSUS_API_KEY")
    if key:
        params["key"] = key
    resp = requests.get(url, params=params, timeout=120)
    resp.raise_for_status()
    if "json" not in resp.headers.get("Content-Type", ""):
        pytest.skip("Census API returned a non-JSON page (keyless limit?); set CENSUS_API_KEY")
    return resp.json()


def _sidecar(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_national_relationship_file_unchanged():
    meta = _sidecar(gc.RELATIONSHIP_SIDECAR)
    resp = requests.get(meta["source_url"], timeout=300)
    resp.raise_for_status()
    first = resp.content.split(b"\n", 1)[0].decode("utf-8-sig")
    for col in ("GEOID_TRACT_20", "GEOID_TRACT_10", "AREALAND_PART", "AREAWATER_PART"):
        assert col in first.split("|")
    assert hashlib.sha256(resp.content).hexdigest() == meta["source_sha256"], (
        "Census republished the relationship file; rerun scripts/fetch_tract_relationships.py"
    )


def test_connecticut_file_unchanged():
    meta = _sidecar(gc.CT_RECODE_SIDECAR)
    resp = requests.get(meta["source_url"], timeout=120)
    resp.raise_for_status()
    assert hashlib.sha256(resp.content).hexdigest() == meta["source_sha256"]


def test_api_2022_tracts_use_planning_region_codes():
    """ACS 2022 5-year Connecticut tract GEOIDs should all be in the bundled recode."""
    params = {"get": "NAME,B01003_001E", "for": "tract:*", "in": "state:09"}
    rows = _api_rows("https://api.census.gov/data/2022/acs/acs5", params)
    head = rows[0]
    geoids = {r[head.index("state")] + r[head.index("county")] + r[head.index("tract")] for r in rows[1:]}
    known = set(gc.load_ct_tract_recode()["geoid_2022"])
    land = {g for g in geoids if not g[5:].startswith("99")}
    assert land <= known


@pytest.mark.parametrize("state,county", [("36", "053"), ("36", "065"), ("04", "019"), ("46", "102"), ("02", "158"), ("51", "019")])
def test_api_2019_tracts_match_recoded_2010_codes(state, county):
    """Every 2019 ACS tract GEOID maps to a 2010 Census tract via the renumbering tables."""
    params = {"get": "NAME", "for": "tract:*", "in": f"state:{state} county:{county}"}
    rows = _api_rows("https://api.census.gov/data/2019/acs/acs5", params)
    head = rows[0]
    geoids = {r[head.index("state")] + r[head.index("county")] + r[head.index("tract")] for r in rows[1:]}
    census2010 = set(gc.load_tract_relationships()["geoid_2010"])
    unmatched = {g for g in geoids if gc.acs_to_census2010_geoid(g) not in census2010}
    assert unmatched <= set(gc.ACS_2010_BOUNDARY_CHANGES), sorted(unmatched)
