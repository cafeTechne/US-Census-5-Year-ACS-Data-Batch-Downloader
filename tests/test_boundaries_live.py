# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live Census boundary download (opt-in).

Skipped unless CENSUS_LIVE_TESTS=1. Downloads the 2023 cartographic boundary
tract file for the District of Columbia into a temporary cache and checks it
against the frozen fixture in tests/fixtures/geo/2023/.

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_boundaries_live.py
"""

import json
import os

import pytest

from census import boundaries as B

LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "geo", "2023", "cb_2023_11_tract_500k.zip")

pytestmark = pytest.mark.skipif(not LIVE, reason="set CENSUS_LIVE_TESTS=1 to run live boundary tests")


def test_download_dc_tracts(tmp_path):
    g = B.get_boundaries("tract", 2023, state="11", cache_dir=str(tmp_path))
    assert g.attrs["source_url"] == B.boundary_url("tract", 2023, "11")
    assert len(g) == 206 and g["GEOID"].str.startswith("11001").all()
    assert g.crs.to_epsg() == 4269
    with open(g.attrs["cache_path"] + ".json", encoding="utf-8") as f:
        side = json.load(f)
    assert side["sha256"] == g.attrs["sha256"]
    # A second call is served from the cache (the bogus session is never used).
    again = B.get_boundaries("tract", 2023, state="11", cache_dir=str(tmp_path), session=object())
    assert again.attrs["sha256"] == side["sha256"]
    with open(FIXTURE + ".json", encoding="utf-8") as f:
        fixture = json.load(f)
    if side["sha256"] != fixture["sha256"]:
        pytest.skip("Census republished the file; refresh tests/fixtures/geo")
