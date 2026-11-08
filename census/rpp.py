# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cost-of-living adjustment between places with BEA Regional Price Parities.

PLAN_ROUND_10.md workstream E; decision 5: optional, never applied by
default, and results say when RPPs were used (:func:`describe_place_adjustment`,
``attrs['rpp_adjustment']`` on frames).

RPPs (U.S. = 100) measure price-level differences across places within a
year. Converting nominal dollars to national-average purchasing power::

    place_adjusted = nominal * 100 / RPP(area, year)

Combined with R-CPI-U-RS for time (census.inflation), dollars comparable
across places and years are::

    real = nominal * 100 / RPP(area, year) * INDEX(base_year) / INDEX(year)

Coverage (data/price_parities/rpp.csv, from scripts/update_price_parities.py,
checksummed in rpp.json; verified at load when read from the package's own
data directory, see census.data_integrity): states, state metropolitan and nonmetropolitan
portions, MSAs (OMB 2023 delineation), and the U.S. nonmetropolitan portion,
2008 onward. Areas outside an MSA use the state nonmetropolitan RPP (which
BEA builds from micropolitan and rural counties); counties, tracts and places
are not covered directly, so callers pass the containing MSA or state.

RPPs are estimates with their own error, which BEA does not publish as
standard errors and which is not in ACS MOEs; adjusted MOEs here scale the
ACS MOE only.

BEA's real personal income for states (table SARPI) is
PI / (RPP/100) / (PCE price index/100), with the state results scaled by a
common factor per year so they sum to U.S. real personal income (verified to
6e-6 against SARPI 2008 in tests/test_rpp.py). :func:`adjust_for_place_and_time`
with ``cpi_table=load_pce_price_index()`` and ``base_year=2017`` gives the
unscaled per-area value.
"""

import csv
import io
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from census.data_integrity import clear_verification_cache, read_table_bytes, same_directory
from census.geo import get_resource_path
from census.inflation import DEFAULT_PRICE_INDEX, INDEX_DISPLAY, inflation_factor

GEO_TYPES = ("us", "us_nonmetro", "state", "state_metro", "state_nonmetro", "msa")
COMPONENTS = ("all_items", "goods", "housing", "utilities", "other")
RPP_ESTIMATION_ERROR_NOTE = (
    "RPPs are BEA estimates with their own estimation error (not published as standard errors); that error "
    "is not included in ACS MOEs or in adjusted MOEs."
)
# BEA's MSA RPPs (every year, as revised) use the OMB bulletin 23-01 delineation (rpp.json
# "msa_delineation"). The Census 2024 CBSA boundary file carries it (checked 2026-11-08: its 393
# metropolitan CBSAs are BEA's 387 MSAs plus 6 in Puerto Rico, which have no RPP).
RPP_CBSA_BOUNDARY_YEAR = 2024


class RPPUnavailable(KeyError):
    """No RPP for the requested area or year."""


def bundled_price_parity_dir() -> str:
    """The package's own price parity directory (the only one whose CSVs are checksum-verified)."""
    return get_resource_path(os.path.join("data", "price_parities"))


def price_parity_dir() -> str:
    return os.getenv("CENSUS_PRICE_PARITY_DIR") or bundled_price_parity_dir()


def _sidecar() -> Dict[str, Any]:
    path = os.path.join(price_parity_dir(), "rpp.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def _read_table(name: str) -> Tuple[Optional[List[Dict[str, str]]], Optional[Dict[str, Any]]]:
    """(rows of ``name``, custom-table record); rows None when missing, record None for bundled data."""
    directory = price_parity_dir()
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return None, None
    bundled = same_directory(directory, bundled_price_parity_dir())
    expected = (_sidecar().get("csv_sha256") or {}).get(name) if bundled else None
    raw, digest = read_table_bytes(path, bundled, expected, os.path.join(directory, "rpp.json"),
                                   "scripts/update_price_parities.py")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))
    custom = None if bundled else {"path": os.path.abspath(path), "sha256": digest, "verified": False}
    return rows, custom


@lru_cache(maxsize=None)
def _load() -> Tuple[Dict[Tuple[str, int], Dict[str, Any]], Dict[str, Tuple[str, str]]]:
    rows, _ = _read_table("rpp.csv")
    if rows is None:
        return {}, {}
    table: Dict[Tuple[str, int], Dict[str, Any]] = {}
    names: Dict[str, Tuple[str, str]] = {}
    for r in rows:
        rec = {k: (float(v) if k.startswith("rpp_") and v != "" else (None if k.startswith("rpp_") else v))
               for k, v in r.items()}
        table[(r["geo_fips"], int(r["year"]))] = rec
        names[r["geo_fips"]] = (r["geo_name"], r["geo_type"])
    return table, names


@lru_cache(maxsize=None)
def price_parity_info() -> Dict[str, Any]:
    """Sidecar metadata (rpp.json), or {} when missing.

    With a custom CENSUS_PRICE_PARITY_DIR, ``custom_tables`` maps each CSV
    present to its path and SHA-256 (``verified: False``: nothing was checked).
    """
    info = _sidecar()
    custom = {n: c for n in ("rpp.csv", "pce_price_index.csv") for c in [_read_table(n)[1]] if c is not None}
    if custom:
        info = {**info, "custom_tables": custom}
    return info


def load_pce_price_index() -> Dict[int, float]:
    """National PCE price index (2017=100), same BEA vintage as the RPPs."""
    rows, _ = _read_table("pce_price_index.csv")
    if rows is None:
        return {}
    return {int(r["year"]): float(r["value"]) for r in rows}


def clear_cache() -> None:
    _read_table.cache_clear()
    _load.cache_clear()
    price_parity_info.cache_clear()
    clear_verification_cache()


def load_price_parities() -> pd.DataFrame:
    """The full RPP table as a frame (empty when the data file is missing)."""
    table, _ = _load()
    df = pd.DataFrame(list(table.values()))
    if len(df):
        df["year"] = df["year"].astype(int)
    return df


def _years() -> List[int]:
    return sorted({y for _, y in _load()[0]})


def _state_by_name(name: str) -> Optional[str]:
    _, names = _load()
    key = name.strip().lower()
    for fips, (n, t) in names.items():
        if t == "state" and n.lower() == key:
            return fips[:2]
    return None


def _resolve(geography: Any, state: Optional[str], portion: Optional[str]) -> Tuple[str, List[str]]:
    """GEO FIPS key in rpp.csv for a geography spec, plus advisories."""
    _, names = _load()
    advisories: List[str] = []
    g = str(geography).strip()
    low = g.lower()
    if low in ("us", "u.s.", "united states", "00000", "national"):
        return "00000", advisories
    m = re.fullmatch(r"(msa|cbsa)\s*[:=]?\s*(\d{5})", low)
    if m:
        g = m.group(2)
    elif not g.isdigit():
        st = _state_by_name(g)
        if st is None:
            raise RPPUnavailable(f"unrecognized geography {geography!r}: pass a state FIPS/name, a 5-digit CBSA code, "
                                 "'us', or a state with portion='nonmetro'")
        g = st
    if len(g) == 2:
        if portion in (None, "all"):
            return f"{g}000", advisories
        key = f"{g}{'999' if portion == 'nonmetro' else '998'}"
        if key not in names:
            raise RPPUnavailable(f"BEA publishes no {portion}politan-portion RPP for state {g} "
                                 "(no such portion exists); use the state RPP")
        return key, advisories
    if len(g) == 5:
        if g in names:
            return g, advisories
        # A CBSA code outside BEA's MSA list (micropolitan area or not an MSA): use state nonmetro.
        st = None if state is None else (state if str(state).isdigit() else _state_by_name(str(state)))
        if st is not None and f"{str(st).zfill(2)}999" in names:
            advisories.append(
                f"RPP COVERAGE: {g} is not a BEA metropolitan statistical area; using the state "
                f"{str(st).zfill(2)} nonmetropolitan-portion RPP (BEA nonmetro portions include micropolitan "
                "counties)")
            return f"{str(st).zfill(2)}999", advisories
        raise RPPUnavailable(f"{g} is not a BEA MSA; pass state= to use the state nonmetropolitan RPP")
    raise RPPUnavailable(f"unrecognized geography {geography!r}")


def place_parity(geography: Any, year: int, state: Optional[str] = None, portion: Optional[str] = None,
                 component: str = "all_items") -> Dict[str, Any]:
    """RPP lookup with provenance: {"rpp", "geo_fips", "geo_name", "geo_type", "year", "advisories", ...}.

    ``geography``: "us"; a state FIPS ("06"), "06000" or name ("California");
    a 5-digit CBSA code ("31080" or "msa:31080"); or a BEA portion code
    ("06999" nonmetro, "06998" metro). ``portion``: "metro"/"nonmetro" with a
    state. ``state``: fallback for CBSA codes that are not BEA MSAs.
    ``component``: one of COMPONENTS.
    """
    table, names = _load()
    if not table:
        raise RPPUnavailable(f"no RPP data in {price_parity_dir()}; run scripts/update_price_parities.py")
    if component not in COMPONENTS:
        raise ValueError(f"component must be one of {COMPONENTS}")
    key, advisories = _resolve(geography, state, portion)
    if key not in names:
        raise RPPUnavailable(f"no BEA RPP for {geography!r}")
    rec = table.get((key, int(year)))
    if rec is None or rec.get(f"rpp_{component}") is None:
        ys = _years()
        raise RPPUnavailable(f"no RPP for {names[key][0]} in {year}; BEA RPPs cover {ys[0]}-{ys[-1]} "
                             "(some MSAs start later)")
    info = price_parity_info()
    out = {"rpp": float(rec[f"rpp_{component}"]), "component": component, "geo_fips": key,
           "geo_name": names[key][0], "geo_type": names[key][1], "year": int(year), "advisories": advisories,
           "source": "BEA Regional Price Parities (SARPP/PARPP/MARPP); " + "; ".join(
               n for n in ((info.get("sources") or {}).get("SARPP") or {}).get("bea_notes", [])
               if "updated" in n.lower())}
    custom = (info.get("custom_tables") or {}).get("rpp.csv")
    if custom is not None:
        out["custom_table"] = custom
    return out


def county_rpp_area(county_geoid: str, cbsa: Optional[str] = None, lsad: Optional[str] = None) -> str:
    """BEA RPP area (rpp.csv GEO FIPS) for a county: its MSA when the county is in a
    metropolitan CBSA (``lsad == "M1"``), else its state's nonmetropolitan portion
    (BEA nonmetro portions are built from micropolitan and rural counties). CBSA
    membership must follow BEA's delineation (:data:`RPP_CBSA_BOUNDARY_YEAR`)."""
    county = str(county_geoid).strip()
    if len(county) != 5 or not county.isdigit():
        raise ValueError(f"county GEOID must be 5 digits, got {county_geoid!r}")
    if lsad == "M1" and cbsa:
        return str(cbsa).zfill(5)
    return f"{county[:2]}999"


def adjust_for_place(value: float, geography: Any, year: int, state: Optional[str] = None,
                     portion: Optional[str] = None, component: str = "all_items") -> float:
    """Nominal year-``year`` dollars in ``geography`` -> national-average purchasing power (value*100/RPP)."""
    return float(value) * 100.0 / place_parity(geography, year, state, portion, component)["rpp"]


def adjust_for_place_and_time(value: float, geography: Any, year: int, base_year: int,
                              state: Optional[str] = None, portion: Optional[str] = None,
                              index: str = DEFAULT_PRICE_INDEX, cpi_table: Optional[Mapping[int, float]] = None,
                              allow_provisional: bool = False, rpp_year: Optional[int] = None) -> float:
    """Dollars comparable across places and years: value*100/RPP(year) * INDEX(base)/INDEX(year).

    Time adjustment uses census.inflation (R-CPI-U-RS by default; pass
    ``cpi_table`` for another national index, e.g. load_pce_price_index() to
    match BEA real personal income). ``rpp_year``: use another year's RPP
    (e.g. the latest available) when the dollar year is outside RPP coverage;
    the caller should say so in its result notes.
    """
    rpp = place_parity(geography, rpp_year or year, state, portion)["rpp"]
    return float(value) * 100.0 / rpp * inflation_factor(year, base_year, cpi_table, index, allow_provisional)


def describe_place_adjustment(geography: Any, year: int, state: Optional[str] = None,
                              portion: Optional[str] = None, base_year: Optional[int] = None,
                              index: str = DEFAULT_PRICE_INDEX, acs_period_years: int = 5) -> Dict[str, Any]:
    """The note a result must carry when RPPs were used (decision 5), plus advisories."""
    p = place_parity(geography, year, state, portion)
    notes = [f"PLACE ADJUSTMENT: dollars divided by the BEA Regional Price Parity for {p['geo_name']} "
             f"({p['year']}: {p['rpp']:.3f}, U.S.=100), i.e. expressed in national-average purchasing power."]
    if base_year is not None:
        notes.append(f"Time adjustment to {base_year} dollars with {INDEX_DISPLAY.get(index, index)}.")
    if acs_period_years > 1:
        notes.append(f"ACS {acs_period_years}-year dollars are in end-year ({year}) dollars; the end-year RPP is "
                     "applied to the whole period.")
    notes += p["advisories"]
    if p.get("custom_table"):
        notes.append(f"CUSTOM RPP TABLE: {p['custom_table']['path']} (SHA-256 {p['custom_table']['sha256']}), "
                     "supplied through CENSUS_PRICE_PARITY_DIR and not checked against the bundled checksums.")
    notes.append(RPP_ESTIMATION_ERROR_NOTE)
    return {**p, "notes": notes, "applied_by_default": False}


def adjust_dataframe_for_place(df: pd.DataFrame, columns: Iterable[str], year: int, geography_col: str,
                               state_col: Optional[str] = None, portion: Optional[str] = None,
                               base_year: Optional[int] = None, index: str = DEFAULT_PRICE_INDEX,
                               cpi_table: Optional[Mapping[int, float]] = None) -> pd.DataFrame:
    """Place- (and optionally time-) adjust dollar columns row by row; adds an ``rpp`` column.

    Rows whose geography has no RPP keep NaN and are listed in
    ``attrs['rpp_adjustment']['unavailable']``. MOE columns in ``columns``
    scale by the same factor (RPP error excluded; see RPP_ESTIMATION_ERROR_NOTE).
    """
    out = df.copy()
    factor = inflation_factor(year, base_year, cpi_table, index) if base_year is not None else 1.0
    rpps, unavailable, advisories = [], [], set()
    for _, row in out.iterrows():
        try:
            p = place_parity(row[geography_col], year, None if state_col is None else row[state_col], portion)
            rpps.append(p["rpp"])
            advisories.update(p["advisories"])
        except RPPUnavailable as e:
            rpps.append(float("nan"))
            unavailable.append({"geography": row[geography_col], "reason": str(e)})
    out["rpp"] = rpps
    for c in columns:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce") * 100.0 / out["rpp"] * factor
    out.attrs["rpp_adjustment"] = {
        "applied": True, "year": year, "base_year": base_year,
        "time_index": INDEX_DISPLAY.get(index, index) if base_year is not None else None,
        "columns": list(columns), "unavailable": unavailable, "advisories": sorted(advisories),
        "notes": [RPP_ESTIMATION_ERROR_NOTE],
    }
    return out
