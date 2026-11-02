"""CPI-U inflation adjustment and longitudinal comparability advisories.

ACS 5-year dollar values are expressed in dollars of the period's final year
(the 2018-2022 ACS reports 2022 dollars). To compare across periods, convert to
a common base year:

    real_base = nominal_t * CPI_base / CPI_t
"""

import re
from typing import Dict, Iterable, List, Mapping, Optional

import pandas as pd

from census.stats import parse_variable_code

# BLS CPI-U, U.S. city average, all items, not seasonally adjusted, annual
# average (series CUUR0000SA0; 1982-84 = 100).
CPI_U_ANNUAL: Dict[int, float] = {
    2000: 172.2,
    2001: 177.1,
    2002: 179.9,
    2003: 184.0,
    2004: 188.9,
    2005: 195.3,
    2006: 201.6,
    2007: 207.342,
    2008: 215.303,
    2009: 214.537,
    2010: 218.056,
    2011: 224.939,
    2012: 229.594,
    2013: 232.957,
    2014: 236.736,
    2015: 237.017,
    2016: 240.007,
    2017: 245.120,
    2018: 251.107,
    2019: 255.657,
    2020: 258.811,
    2021: 270.970,
    2022: 292.655,
    2023: 304.702,
    2024: 313.689,
}

CPI_SOURCE = (
    "BLS CPI-U, U.S. city average, all items, annual average (CUUR0000SA0)"
)

_DOLLAR_RE = re.compile(r"\bdollars\b", re.IGNORECASE)

# ACS 5-year datasets: tract and other small-area geographies were redrawn for
# the 2020 Census and first appear in the 2020 (2016-2020) ACS 5-year release.
BOUNDARY_VINTAGE_BREAK = 2020
BOUNDARY_SENSITIVE_LEVELS = {"tract", "zcta", "place", "county", "msa"}


def get_cpi(year: int, cpi_table: Optional[Mapping[int, float]] = None) -> float:
    table = cpi_table or CPI_U_ANNUAL
    if year not in table:
        raise KeyError(
            f"No CPI-U value for {year}. Available years: "
            f"{min(table)}-{max(table)}. Pass cpi_table= to supply newer values."
        )
    return float(table[year])


def inflation_factor(
    from_year: int, to_year: int, cpi_table: Optional[Mapping[int, float]] = None
) -> float:
    """Multiplier converting from_year dollars into to_year dollars."""
    return get_cpi(to_year, cpi_table) / get_cpi(from_year, cpi_table)


def adjust_inflation(
    value: float,
    from_year: int,
    to_year: int,
    cpi_table: Optional[Mapping[int, float]] = None,
) -> float:
    """Convert a nominal dollar value from from_year dollars to to_year dollars."""
    return float(value) * inflation_factor(from_year, to_year, cpi_table)


def is_dollar_variable(label: str, concept: str = "") -> bool:
    """True when a variable's *value* is a dollar amount (not a count in a $ bracket).

    Census labels put dollar context in parent headings and concepts, e.g.
    "INCOME AND BENEFITS (IN 2022 INFLATION-ADJUSTED DOLLARS)!!Total
    households!!Less than $10,000" is a household count. So only the leaf
    label decides, plus tables whose concept is itself a dollar statistic
    (MEDIAN GROSS RENT (DOLLARS), MEDIAN HOUSEHOLD INCOME ... BY AGE).
    """
    if not label:
        return False
    leaf = label.split("!!")[-1].strip()
    if "$" in leaf or re.search(r"percent|rate\b|ratio", leaf, re.IGNORECASE):
        return False
    if _DOLLAR_RE.search(leaf):  # "(dollars)", "(in 2021 inflation-adjusted dollars)"
        return True
    stat = re.compile(r"^(median|mean|aggregate|average|per capita)\b", re.IGNORECASE)
    if stat.match(leaf) and _DOLLAR_RE.search(f"{label} {concept}"):
        return True
    concept = concept.strip()
    return bool(stat.match(concept) and _DOLLAR_RE.search(concept))


def dollar_columns(
    columns: Iterable[str], metadata: Mapping[str, Mapping[str, str]]
) -> List[str]:
    """Estimate/MOE columns (E, M) whose variable is dollar-denominated.

    ``metadata`` maps variable codes to {"label", "concept"}. The estimate's
    metadata decides for its MOE too. Percent columns are never selected.
    """
    selected: List[str] = []
    for col in columns:
        parsed = parse_variable_code(str(col))
        if not parsed or parsed[1] not in ("E", "M"):
            continue
        entry = metadata.get(f"{parsed[0]}E") or {}
        if is_dollar_variable(str(entry.get("label", "")), str(entry.get("concept", ""))):
            selected.append(col)
    return selected


def deflate_dataframe(
    df: pd.DataFrame,
    year: int,
    base_year: int,
    columns: Iterable[str],
    cpi_table: Optional[Mapping[int, float]] = None,
) -> pd.DataFrame:
    """Scale the given dollar columns from ``year`` dollars into ``base_year`` dollars.

    MOEs scale by the same factor, so they should be included in ``columns``.
    """
    out = df.copy()
    factor = inflation_factor(year, base_year, cpi_table)
    for col in columns:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") * factor
    return out


def boundary_shift_advisory(
    start_year: int, end_year: int, geo_level: str = ""
) -> Optional[str]:
    """Warn when a multi-year request spans the 2010 -> 2020 boundary vintages."""
    if not (start_year < BOUNDARY_VINTAGE_BREAK <= end_year):
        return None
    if geo_level and geo_level not in BOUNDARY_SENSITIVE_LEVELS:
        return None  # state / national boundaries are stable
    detail = {
        "tract": "Many census tracts were split, merged, or renumbered; tract GEOIDs "
        "before and after 2020 do not describe the same areas. Use a crosswalk "
        "(e.g. Census tract relationship files or NHGIS/LTDB) before panel analysis.",
        "zcta": "ZCTAs were re-delineated from 2020 Census address data; some ZCTAs "
        "were added, dropped, or changed shape.",
        "place": "Place boundaries are updated annually and were re-delineated for "
        "2020 (notably CDPs); compare with caution.",
        "county": "A few counties changed (e.g. Connecticut planning regions from "
        "2022; Alaska boroughs); most counties are stable.",
        "msa": "CBSA delineations changed (OMB 2013, 2018, 2020, 2023 bulletins); "
        "metro definitions may differ across years.",
    }.get(geo_level, "")
    msg = (
        f"BOUNDARY ADVISORY: The requested years {start_year}-{end_year} span the "
        f"2019/2020 ACS 5-year boundary vintage change (2010 Census geography -> "
        f"2020 Census geography)."
    )
    return f"{msg} {detail}".strip()
