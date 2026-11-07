# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reproduce Census PUMS verification estimates (PLAN_ROUND_10 workstream B).

Offline: frozen Wyoming fixtures (tests/fixtures/pums_validation/) for the
ACS 2024 1-year and 2020-2024 5-year PUMS, with the published verification
rows copied from pums_estimates_24.csv / pums_estimates_20_24.csv. Every one
of the 32 characteristics (estimate, SE and 90% MOE) must match within the
rounding of the published integers.

Live (opt-in, downloads ~8 MB):

    CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_pums_validation.py
"""

import hashlib
import json
import os

import pandas as pd
import pytest

from census import pums as P
from census import pums_validation as V

FX = os.path.join(os.path.dirname(__file__), "fixtures", "pums_validation")
LIVE = os.getenv("CENSUS_LIVE_TESTS") == "1"

with open(os.path.join(FX, "fixtures.json"), encoding="utf-8") as _f:
    META = json.load(_f)

RELEASES = [("2024_1yr", 2024, "1-year", "published_pums_estimates_24.csv"),
            ("2020_2024_5yr", 2024, "5-year", "published_pums_estimates_20_24.csv")]


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _load(tag, published):
    with open(os.path.join(FX, published), "rb") as f:
        pub = V.parse_verification(f.read(), "56")
    person = pd.read_csv(os.path.join(FX, f"person_cells_{tag}.csv"))
    housing = pd.read_csv(os.path.join(FX, f"housing_cells_{tag}.csv"))
    return pub, person, housing


@pytest.mark.parametrize("tag,year,product,published", RELEASES)
def test_fixture_hashes_and_urls(tag, year, product, published):
    rel = META["releases"][tag]
    for name, digest in rel["files"].items():
        assert _sha(os.path.join(FX, name)) == digest
    assert rel["verification_url"] == V.verification_url(year, product)
    assert rel["person_url"] == P.pums_url("56", year, product, "person")
    assert rel["housing_url"] == P.pums_url("56", year, product, "housing")


@pytest.mark.parametrize("tag,year,product,published", RELEASES)
def test_reproduces_verification_estimates(tag, year, product, published):
    pub, person, housing = _load(tag, published)
    res = V.reproduce(pub, person, housing)
    s = V.summarize(res)
    assert s["characteristics"] == 32 and s["unmapped"] == []
    assert s["matched"] == 32, res.loc[res.status != "match"].to_string()
    assert s["max_abs_error_estimate"] <= V.ROUNDING_TOLERANCE
    assert s["max_abs_error_se"] <= V.ROUNDING_TOLERANCE
    assert s["max_abs_error_moe"] <= V.ROUNDING_TOLERANCE
    # The record-level reproduction recorded when the fixtures were built agrees.
    rec = META["releases"][tag]["reproduction_from_records"]
    assert rec["matched"] == 32
    assert s["max_abs_error_se"] == pytest.approx(rec["max_abs_error_se"], abs=1e-6)
    # Nonzero SEs are actually exercised (not only controlled totals).
    assert (res["published_se"] > 100).sum() >= 20


def test_verification_urls():
    base = "https://www2.census.gov/programs-surveys/acs/tech_docs/pums/estimates/"
    assert V.verification_url(2024, "1-year") == base + "pums_estimates_24.csv"
    assert V.verification_url(2024, "5-year") == base + "pums_estimates_20_24.csv"
    assert V.verification_url(2023, "5-year") == base + "pums_estimates_19_23.csv"


def test_unknown_characteristic_is_reported_not_guessed():
    pub = pd.DataFrame([{"STATE": "56", "STATENAME": "Wyoming", "CHARACTERISTIC": "Something new",
                         "published_estimate": 1, "published_se": 0, "published_moe": 0}])
    res = V.reproduce(pub, None, None)
    assert res.status.tolist() == ["unmapped"]


@pytest.mark.skipif(not LIVE, reason="set CENSUS_LIVE_TESTS=1 to run live PUMS verification")
@pytest.mark.parametrize("tag,year,product,published", RELEASES)
def test_live_wyoming(tmp_path, tag, year, product, published):
    res, s = V.validate_release("56", year, product, cache_dir=str(tmp_path))
    rel = META["releases"][tag]
    assert s["matched"] == 32, res.loc[res.status != "match"].to_string()
    assert s["verification_sha256"] == rel["verification_sha256"]
    assert s["person_sha256"] == rel["person_sha256"] and s["housing_sha256"] == rel["housing_sha256"]
