# SPDX-License-Identifier: AGPL-3.0-or-later
"""2010 <-> 2020 census tract crosswalks and apportionment of ACS estimates.

Tract vintages of ACS 5-year releases
-------------------------------------
"For ACS 5-year estimates, use the last year of the estimate period to
determine the vintage" (Census Bureau, *Geography Boundaries by Year*,
https://www.census.gov/programs-surveys/acs/geography-acs/geography-boundaries-by-year.html).
Census tracts are "2010 Census" vintage through the 2019 release and "2020
Census" vintage from the 2020 release on (same site, pages ``/2019.html`` and
``/2020.html``). The 2009 release used 2000 tracts and is not supported.

Two code changes inside a vintage matter for joining ACS GEOIDs to the
relationship file, which uses decennial-census codes:

* **2010 vintage.** After the 2010 Census, the 2011 and 2012 ACS renumbered
  tracts in Madison and Oneida counties, NY and Pima County, AZ (ACS "Geography
  Changes" 2011 and 2012), and county codes changed for Shannon County, SD
  (46113 -> Oglala Lakota 46102, 2015), Wade Hampton, AK (02270 -> Kusilvak
  02158, 2015) and Bedford city, VA (51515, absorbed by Bedford County 51019,
  2013) (Census, *Substantial Changes to Counties*, 2010s). These pure recodes
  are applied by :func:`harmonize_panel`. Three post-2010 *boundary* changes
  (Los Angeles 06037137000; Oneida 36065024900 / 36065023000) cannot be
  recoded and are reported, not guessed.
* **2020 vintage, Connecticut.** From the 2022 release, ACS tabulates
  Connecticut by nine planning regions (county codes 09110-09190) instead of
  eight counties (ACS user note 2023-01,
  https://www.census.gov/programs-surveys/acs/technical-documentation/user-notes/2023-01.html).
  Tract delineations are unchanged but tract GEOIDs differ between the 2020/2021
  and 2022+ releases. ``data/geo_crosswalks/ct_tract_2020_2022.csv`` (derived
  by ``scripts/fetch_tract_relationships.py``) recodes between them.

Method
------
Weights come from the Census Bureau's *2020 Census Tract to 2010 Census Tract
Relationship File* (``data/geo_crosswalks/tract_2010_2020.csv.gz``; source,
retrieval date and SHA-256 in the JSON sidecar). With ``weight="land_area"``
the weight of source tract s on target tract t is

    w(s, t) = AREALAND_PART(s, t) / sum_t' AREALAND_PART(s, t')

so the weights of each source sum to 1. A source with no land (water-only
tracts) falls back to total area (land + water) of the parts, and a source
with no area at all to an equal split; both are counted in the crosswalk's
``attrs["fallbacks"]``. Land-area weights assume people are spread evenly over
each source tract's land; population-based weights (for example NHGIS
block-based crosswalks, which have their own terms of use) are more accurate
and can be supplied with :func:`load_weight_file`.

Apportioned estimate and MOE (method ``apportioned``):

    X_t   = sum_s w(s, t) X_s
    MOE_t = sqrt(sum_s (w(s, t) MOE_s)^2)

This treats source estimates as independent and the weights as known
constants; weight error is ignored, so MOEs are understated when weights are
poor. Zero-estimate MOEs follow ``census.derived`` (``zero_estimate_rule``).

Only additive counts (and dollar aggregates) can be apportioned. Medians,
means, percentages, rates, ratios and per-capita values are refused: apportion
the underlying counts and re-derive the statistic with ``census.derived``.
"""

import functools
import gzip
import hashlib
import io
import json
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from .stats import ACS_MOE_Z, CENSUS_SENTINELS, parse_variable_code, reliability_flag
from .variance import METHOD_APPORTIONED

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data", "geo_crosswalks")
RELATIONSHIP_FILE = os.path.join(DATA_DIR, "tract_2010_2020.csv.gz")
RELATIONSHIP_SIDECAR = os.path.join(DATA_DIR, "tract_2010_2020.json")
CT_RECODE_FILE = os.path.join(DATA_DIR, "ct_tract_2020_2022.csv")
CT_RECODE_SIDECAR = os.path.join(DATA_DIR, "ct_tract_2020_2022.json")

DIRECTIONS = ("2010_to_2020", "2020_to_2010")
WEIGHT_LAND = "land_area"
WEIGHT_TOTAL = "total_area"
WEIGHT_POPULATION = "population"
AREA_WEIGHTS = (WEIGHT_LAND, WEIGHT_TOTAL)

CROSSWALK_COLUMNS = ["source_geoid", "target_geoid", "weight"]
TRACT_GEOID_LENGTH = 11
CONTROLLED_MOE = -555555555
ZERO_RULES = ("all", "largest")
NON_ADDITIVE_POLICIES = ("raise", "drop")
DEFAULT_MIN_COVERAGE = 0.99

# First ACS 5-year release on each tract vintage.
FIRST_RELEASE_2010_TRACTS = 2010
FIRST_RELEASE_2020_TRACTS = 2020
FIRST_RELEASE_CT_PLANNING_REGIONS = 2022

NOTE_INDEPENDENCE = (
    "MOE = sqrt(sum (w * MOE)^2) treats source tract estimates as independent and the "
    "weights as fixed; weight uncertainty is ignored"
)
NOTE_LAND = "land-area weights assume a uniform distribution over each source tract's land"
LIMITATIONS = [
    NOTE_INDEPENDENCE,
    "apportioned values are modelled, not Census-published, and are not exact for any "
    "tract that was split or merged",
    "only additive counts and dollar aggregates are apportioned; derive rates, "
    "percentages, means and medians afterwards from apportioned counts",
]

# ---------------------------------------------------------------------------
# GEOID recodes
# ---------------------------------------------------------------------------

# 2010 Census tract GEOID -> GEOID used by ACS releases 2011/2012-2019 (pure
# renumbering; geography unchanged). Sources: ACS "Geography Changes" 2011
# (https://www.census.gov/programs-surveys/acs/technical-documentation/table-and-geography-changes/2011/geography-changes.html)
# and 2012 (.../2012/geography-changes.html).
ACS_2010_TRACT_RENUMBERING: Dict[str, str] = {
    # Madison County, NY (2011)
    "36053940101": "36053030101",
    "36053940102": "36053030102",
    "36053940103": "36053030103",
    "36053940200": "36053030200",
    "36053940300": "36053030300",
    "36053940401": "36053030401",
    "36053940403": "36053030403",
    "36053940600": "36053030600",
    "36053940700": "36053030402",
    # Oneida County, NY (2011)
    "36065940000": "36065024800",
    "36065940100": "36065024700",
    # Pima County, AZ (2012)
    "04019002701": "04019002704",
    "04019002903": "04019002906",
    "04019410501": "04019004118",
    "04019410502": "04019004121",
    "04019410503": "04019004125",
    "04019470400": "04019005200",
    "04019470500": "04019005300",
    # Bedford city, VA (51515) became part of Bedford County (51019) in 2013.
    "51515050100": "51019050100",
}
# 2010 Census county prefix -> ACS county prefix (code change, same tracts).
ACS_2010_COUNTY_RECODES: Dict[str, str] = {
    "46113": "46102",  # Shannon County -> Oglala Lakota County, SD (2015)
    "02270": "02158",  # Wade Hampton -> Kusilvak Census Area, AK (2015)
}
# ACS-era GEOIDs whose areas changed after the 2010 Census: not recodable.
ACS_2010_BOUNDARY_CHANGES: Dict[str, str] = {
    "06037137000": "Los Angeles County, CA tract 1370.00 reinstated in 2012 from all of "
    "2010 tract 9304.01 and part of 8002.04",
    "06037800204": "Los Angeles County, CA tract 8002.04 lost area to tract 1370.00 in 2012",
    "36065024900": "Oneida County, NY tract 0249.00 formed in 2011 from 2010 tract "
    "9402.00 and part of 0230.00",
    "36065023000": "Oneida County, NY tract 0230.00 lost area to tract 0249.00 in 2011",
}

GeoidLike = Union[str, int, float]


def normalize_tract_geoid(value: Any) -> str:
    """Return an 11-digit tract GEOID string.

    Restores leading zeros lost to numeric parsing (``1001020100`` ->
    ``"01001020100"``) and strips an API ``GEO_ID`` prefix
    (``1400000US01001020100``). Raises ValueError for anything else.
    """
    text = str(value).strip()
    if "US" in text:
        text = text.split("US", 1)[1]
    if text.endswith(".0"):
        text = text[:-2]
    if not text.isdigit() or len(text) > TRACT_GEOID_LENGTH:
        raise ValueError(f"not a census tract GEOID: {value!r}")
    return text.zfill(TRACT_GEOID_LENGTH)


def normalize_tract_geoids(values: Iterable[Any]) -> pd.Series:
    if isinstance(values, pd.Series):
        return values.map(normalize_tract_geoid)
    return pd.Series([normalize_tract_geoid(v) for v in values], dtype=object)


def tract_vintage(release_year: int) -> int:
    """Census tract vintage (2010 or 2020) of an ACS 5-year release."""
    year = int(release_year)
    if year >= FIRST_RELEASE_2020_TRACTS:
        return 2020
    if year >= FIRST_RELEASE_2010_TRACTS:
        return 2010
    raise ValueError(
        f"ACS 5-year release {year} uses 2000 Census tracts; only 2010 and 2020 "
        "tract vintages are supported"
    )


def acs_to_census2010_geoid(geoid: str) -> str:
    """Map a 2010-vintage ACS tract GEOID to its 2010 Census code (identity if unchanged)."""
    inverse = _acs2010_inverse()
    if geoid in inverse:
        return inverse[geoid]
    for old, new in ACS_2010_COUNTY_RECODES.items():
        if geoid.startswith(new):
            return old + geoid[5:]
    return geoid


def census2010_to_acs_geoid(geoid: str) -> str:
    """Map a 2010 Census tract GEOID to the code used by the latest 2010-vintage ACS release."""
    if geoid in ACS_2010_TRACT_RENUMBERING:
        return ACS_2010_TRACT_RENUMBERING[geoid]
    prefix = ACS_2010_COUNTY_RECODES.get(geoid[:5])
    return prefix + geoid[5:] if prefix else geoid


@functools.lru_cache(maxsize=1)
def _acs2010_inverse() -> Dict[str, str]:
    return {v: k for k, v in ACS_2010_TRACT_RENUMBERING.items()}


@functools.lru_cache(maxsize=4)
def _load_ct_recode_cached(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    if list(df.columns) != ["geoid_2020", "geoid_2022"]:
        raise ValueError(f"{path}: expected columns geoid_2020, geoid_2022")
    return df


def load_ct_tract_recode(path: Optional[str] = None) -> pd.DataFrame:
    """Connecticut tract GEOIDs: 2020 county-based (``geoid_2020``) -> 2022 planning-region (``geoid_2022``)."""
    return _load_ct_recode_cached(os.path.abspath(path or CT_RECODE_FILE)).copy()


def recode_ct_tracts(geoids: Iterable[str], to: str = "2022", path: Optional[str] = None) -> pd.Series:
    """Recode Connecticut tract GEOIDs between 2020/2021 (``"2020"``) and 2022+ (``"2022"``) ACS coding.

    Non-Connecticut GEOIDs and codes already in the target coding pass through.
    Water-only Connecticut tracts (99xxxx) have no recode and pass through.
    """
    if to not in ("2020", "2022"):
        raise ValueError("to must be '2020' or '2022'")
    table = load_ct_tract_recode(path)
    mapping = (
        dict(zip(table["geoid_2020"], table["geoid_2022"]))
        if to == "2022"
        else dict(zip(table["geoid_2022"], table["geoid_2020"]))
    )
    series = geoids if isinstance(geoids, pd.Series) else pd.Series(list(geoids), dtype=object)
    return series.map(lambda g: mapping.get(g, g))


# ---------------------------------------------------------------------------
# Relationship file and crosswalks
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@functools.lru_cache(maxsize=4)
def _load_relationships_cached(path: str, sidecar: Optional[str], verify: bool) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found; run `python scripts/fetch_tract_relationships.py` to build it"
        )
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:  # type: ignore[operator]
        raw = f.read()
    if verify and sidecar and os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as f:
            expected = json.load(f).get("csv_sha256")
        if expected and _sha256(raw) != expected:
            raise ValueError(f"{path}: SHA-256 does not match {os.path.basename(sidecar)}")
    df = pd.read_csv(io.BytesIO(raw), dtype={"geoid_2010": str, "geoid_2020": str})
    for col in df.columns:
        if col.startswith("area"):
            df[col] = df[col].astype("int64")
    return df


def load_tract_relationships(path: Optional[str] = None, verify: bool = True) -> pd.DataFrame:
    """Load the compact 2010/2020 tract relationship extract.

    Columns: ``geoid_2010, geoid_2020, arealand_part, areawater_part,
    arealand_2010, areawater_2010, arealand_2020, areawater_2020`` (square
    meters; GEOIDs as 11-character strings, 2010 and 2020 *Census* codes).
    ``verify`` checks the decompressed CSV against ``csv_sha256`` in the sidecar.
    """
    path = os.path.abspath(path or RELATIONSHIP_FILE)
    stem = path[: -len(".gz")] if path.endswith(".gz") else path
    sidecar = os.path.splitext(stem)[0] + ".json"
    return _load_relationships_cached(path, sidecar if os.path.exists(sidecar) else None, verify).copy()


def relationship_source() -> Dict[str, Any]:
    """Provenance of the bundled relationship extract (from its sidecar)."""
    if not os.path.exists(RELATIONSHIP_SIDECAR):
        return {}
    with open(RELATIONSHIP_SIDECAR, encoding="utf-8") as f:
        meta = json.load(f)
    keep = ("name", "source_url", "retrieved_at", "source_sha256", "csv_sha256", "rows")
    return {k: meta.get(k) for k in keep}


def tract_crosswalk(
    direction: str = "2010_to_2020",
    weight: str = WEIGHT_LAND,
    weights_file: Optional[str] = None,
    relationships: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Weights table ``source_geoid, target_geoid, weight`` (+ ``part_area``).

    ``direction``: ``"2010_to_2020"`` or ``"2020_to_2010"`` (Census codes).
    ``weight``: ``"land_area"`` (default), ``"total_area"`` (land + water), or
    ``"population"`` with ``weights_file`` (see :func:`load_weight_file`).
    Weights of every source tract sum to 1. ``part_area`` is the area of each
    intersection used for weighting; :func:`apportion` uses it to measure how
    much of a target tract the input covers.

    ``attrs``: ``weight_type``, ``direction``, ``fallbacks`` (sources that fell
    back to total area or an equal split), ``notes``.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if weights_file is not None or weight == WEIGHT_POPULATION:
        if weights_file is None:
            raise ValueError(
                "weight='population' needs weights_file= (e.g. an NHGIS block-based tract "
                "crosswalk with columns source_geoid, target_geoid, weight)"
            )
        cw = load_weight_file(weights_file)
        cw.attrs.update(
            weight_type=weight if weight not in AREA_WEIGHTS else "user-supplied",
            direction=direction,
            fallbacks={},
            notes=[f"weights from user-supplied file {os.path.basename(weights_file)}"],
        )
        return cw
    if weight not in AREA_WEIGHTS:
        raise ValueError(f"weight must be one of {AREA_WEIGHTS + (WEIGHT_POPULATION,)}")

    rel = relationships if relationships is not None else load_tract_relationships()
    src, tgt = ("geoid_2010", "geoid_2020") if direction == "2010_to_2020" else ("geoid_2020", "geoid_2010")
    land = rel["arealand_part"].astype(float)
    total = land + rel["areawater_part"].astype(float)
    frame = pd.DataFrame(
        {"source_geoid": rel[src].values, "target_geoid": rel[tgt].values,
         "land": land.values, "total": total.values}
    )
    sums = frame.groupby("source_geoid")[["land", "total"]].transform("sum")
    counts = frame.groupby("source_geoid")["land"].transform("size")
    if weight == WEIGHT_LAND:
        use_land = sums["land"] > 0
    else:
        use_land = pd.Series(False, index=frame.index)
    use_total = ~use_land & (sums["total"] > 0)
    equal = ~use_land & ~use_total
    part = np.where(use_land, frame["land"], np.where(use_total, frame["total"], 1.0))
    denom = np.where(use_land, sums["land"], np.where(use_total, sums["total"], counts))
    frame["weight"] = part / denom
    frame["part_area"] = np.where(equal, 0.0, part)
    out = frame.loc[frame["weight"] > 0, CROSSWALK_COLUMNS + ["part_area"]]
    out = out.sort_values(["source_geoid", "target_geoid"]).reset_index(drop=True)

    n_total = int(frame.loc[use_total & (weight == WEIGHT_LAND), "source_geoid"].nunique())
    n_equal = int(frame.loc[equal, "source_geoid"].nunique())
    notes = [f"{weight} weights from the Census 2020-2010 tract relationship file ({direction})"]
    if weight == WEIGHT_LAND:
        notes.append(NOTE_LAND)
        if n_total:
            notes.append(f"{n_total} source tract(s) with no land (water only) use total-area weights")
    if n_equal:
        notes.append(f"{n_equal} source tract(s) with no area use an equal split")
    out.attrs.update(
        weight_type=weight,
        direction=direction,
        fallbacks={"total_area": n_total, "equal_split": n_equal},
        notes=notes,
        source=relationship_source(),
    )
    return out


def load_weight_file(
    path: str,
    columns: Optional[Mapping[str, str]] = None,
    tolerance: float = 1e-4,
    renormalize: bool = False,
) -> pd.DataFrame:
    """Read and validate a user-supplied crosswalk (e.g. NHGIS population-based).

    The file (CSV, optionally gzipped) must have ``source_geoid,
    target_geoid, weight``; pass ``columns`` to rename, e.g.
    ``{"tr2010ge": "source_geoid", "tr2020ge": "target_geoid", "wt_pop": "weight"}``.
    GEOIDs are zero-padded to 11 digits. Weights must be finite and in [0, 1],
    and each source's weights must sum to 1 within ``tolerance`` (set
    ``renormalize=True`` to rescale sums instead; sources summing to 0 are
    always an error).
    """
    df = pd.read_csv(path, dtype=str)
    if columns:
        df = df.rename(columns=dict(columns))
    missing = [c for c in CROSSWALK_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing column(s) {missing}; need {CROSSWALK_COLUMNS}")
    out = df[CROSSWALK_COLUMNS].copy()
    out["source_geoid"] = normalize_tract_geoids(out["source_geoid"])
    out["target_geoid"] = normalize_tract_geoids(out["target_geoid"])
    out["weight"] = pd.to_numeric(out["weight"], errors="coerce")
    bad = out["weight"].isna() | ~np.isfinite(out["weight"]) | (out["weight"] < 0) | (out["weight"] > 1 + tolerance)
    if bad.any():
        raise ValueError(f"{path}: {int(bad.sum())} weight(s) missing, non-finite, or outside [0, 1]")
    out = out.groupby(["source_geoid", "target_geoid"], as_index=False)["weight"].sum()
    sums = out.groupby("source_geoid")["weight"].transform("sum")
    zero = sorted(out.loc[sums <= 0, "source_geoid"].unique())
    if zero:
        raise ValueError(f"{path}: {len(zero)} source(s) have weights summing to 0, e.g. {zero[:5]}")
    off = sorted(out.loc[(sums - 1).abs() > tolerance, "source_geoid"].unique())
    if off and not renormalize:
        raise ValueError(
            f"{path}: weights of {len(off)} source(s) do not sum to 1 (tolerance {tolerance}), "
            f"e.g. {off[:5]}; pass renormalize=True to rescale"
        )
    out["weight"] = out["weight"] / sums
    out = out.loc[out["weight"] > 0].sort_values(["source_geoid", "target_geoid"]).reset_index(drop=True)
    out.attrs.update(weight_type="user-supplied", renormalized=bool(off), notes=[])
    return out


# ---------------------------------------------------------------------------
# Additivity checks
# ---------------------------------------------------------------------------

_NON_ADDITIVE_RE = re.compile(
    r"\b(median|medians|mean|average|per capita|percent|percentage|rate|rates|ratio|"
    r"gini|index|quartile|quintile|per 1,000|per 100)\b",
    re.I,
)
_NON_ADDITIVE_NAME_RE = re.compile(
    r"(^|_)(median|mean|avg|average|per_?capita|pct|percent|percentage|rate|ratio|gini|index)(_|$)",
    re.I,
)
# Detailed tables whose estimates are medians, means, ratios, or indexes. Used
# even when labels are unavailable.
KNOWN_NON_ADDITIVE_TABLES = frozenset(
    {
        "B01002", "B06011", "B07011", "B07411", "B08103", "B08121", "B08503", "B08521",
        "B19013", "B19019", "B19049", "B19083", "B19113", "B19119", "B19125", "B19202",
        "B19215", "B19301", "B19326", "B20002", "B20004", "B20017", "B20018", "B25010",
        "B25018", "B25021", "B25031", "B25035", "B25037", "B25039", "B25058", "B25064",
        "B25071", "B25076", "B25077", "B25078", "B25088", "B25092", "B25097", "B25105",
        "B25107", "B25109", "B25111", "B25113", "B25119", "B25121",
    }
)


def _table_of(code: str) -> str:
    return code.split("_", 1)[0]


def _non_additive_reason(col: str, metadata: Optional[Mapping[str, Mapping[str, str]]]) -> Optional[str]:
    parsed = parse_variable_code(col)
    if parsed is None:
        if _NON_ADDITIVE_NAME_RE.search(col):
            return "column name suggests a non-additive measure"
        return None
    base, suffix = parsed
    if suffix != "E":
        return f"suffix {suffix} is not an additive count estimate"
    if _table_of(base) in KNOWN_NON_ADDITIVE_TABLES:
        return f"table {_table_of(base)} reports a median, mean, ratio, or index"
    entry = (metadata or {}).get(col) or (metadata or {}).get(base.split("__", 1)[0] + "E")
    if entry:
        label = str(entry.get("label", ""))
        match = _NON_ADDITIVE_RE.search(label)
        if match:
            return f"label {label!r} mentions {match.group(1).lower()!r}"
    return None


def _metadata_for(metadata: Optional[Mapping[str, Mapping[str, str]]]) -> Mapping[str, Mapping[str, str]]:
    if metadata is not None:
        return metadata
    try:  # offline archive of detailed-table labels (latest vintage)
        from .tables import load_variable_metadata

        return load_variable_metadata()
    except Exception:  # pragma: no cover - archive missing
        return {}


def check_additive(
    columns: Sequence[str],
    metadata: Optional[Mapping[str, Mapping[str, str]]] = None,
    assume_additive: Union[bool, Sequence[str]] = False,
) -> Tuple[List[str], Dict[str, str], List[str]]:
    """Split estimate columns into (additive, refused {col: reason}, unverified).

    ``unverified`` lists code columns without a label in ``metadata`` (or the
    offline archive when ``metadata`` is None). ``assume_additive=True`` (or a
    list of columns) accepts unverified columns and overrides label-based
    refusals; known non-additive tables and non-``E`` suffixes are always refused.
    """
    meta = _metadata_for(metadata)
    forced = set(columns) if assume_additive is True else set(assume_additive or [])
    ok: List[str] = []
    refused: Dict[str, str] = {}
    unverified: List[str] = []
    for col in columns:
        reason = _non_additive_reason(col, meta)
        parsed = parse_variable_code(col)
        hard = parsed is not None and (parsed[1] != "E" or _table_of(parsed[0]) in KNOWN_NON_ADDITIVE_TABLES)
        if reason and (hard or col not in forced):
            refused[col] = reason
            continue
        if parsed is not None and col not in forced:
            if not (meta.get(col) or meta.get(parsed[0].split("__", 1)[0] + "E")):
                unverified.append(col)
                continue
        ok.append(col)
    return ok, refused, unverified


def _refusal_message(refused: Mapping[str, str]) -> str:
    items = "; ".join(f"{c}: {r}" for c, r in list(refused.items())[:8])
    more = f" (+{len(refused) - 8} more)" if len(refused) > 8 else ""
    return (
        "Cannot apportion non-additive measures (medians, means, percentages, rates, "
        f"ratios, per-capita values): {items}{more}. Apportion the underlying counts "
        "(numerator and denominator estimates) and re-derive the statistic with "
        "census.derived; or pass non_additive='drop' to leave these columns out."
    )


# ---------------------------------------------------------------------------
# Apportionment
# ---------------------------------------------------------------------------


def _clean_values(values: pd.DataFrame, is_moe: bool) -> pd.DataFrame:
    out = values.apply(pd.to_numeric, errors="coerce").astype(float)
    if is_moe:
        out = out.mask(out == CONTROLLED_MOE, 0.0)
        out = out.mask(out < 0)  # other sentinels / invalid negatives -> undefined
    else:
        out = out.mask(out.isin(list(CENSUS_SENTINELS)))
    return out


def _moe_column(est_col: str, cols: Sequence[str], moe_cols: Optional[Mapping[str, str]]) -> Optional[str]:
    if moe_cols and est_col in moe_cols:
        return moe_cols[est_col]
    parsed = parse_variable_code(est_col)
    if parsed and parsed[1] == "E" and f"{parsed[0]}M" in cols:
        return f"{parsed[0]}M"
    return None


def _apportion_frame(
    df: pd.DataFrame,
    crosswalk: pd.DataFrame,
    pairs: Sequence[Tuple[str, Optional[str]]],
    geoid_col: str,
    group_cols: Sequence[str],
    zero_estimate_rule: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Core: returns (target frame, coverage frame). Rows of df must have unique keys."""
    est_cols = [e for e, _ in pairs]
    moe_pairs = [(e, m) for e, m in pairs if m]
    keep = list(group_cols) + [geoid_col] + est_cols + [m for _, m in moe_pairs]
    src = df[keep].copy()
    src[est_cols] = _clean_values(src[est_cols], is_moe=False)
    if moe_pairs:
        mcols = [m for _, m in moe_pairs]
        src[mcols] = _clean_values(src[mcols], is_moe=True)
    mass_col = "part_area" if "part_area" in crosswalk.columns else "weight"
    cw = crosswalk.rename(columns={"source_geoid": geoid_col})
    cw_cols = [geoid_col, "target_geoid", "weight"] + (["part_area"] if mass_col == "part_area" else [])
    merged = src.merge(cw[cw_cols], on=geoid_col, how="inner")
    keys = list(group_cols) + ["target_geoid"]
    w = merged["weight"].to_numpy()[:, None]

    est = merged[est_cols].to_numpy(dtype=float)
    contrib = pd.DataFrame(est * w, columns=est_cols, index=merged.index)
    grouped = contrib.groupby([merged[k] for k in keys], sort=True)
    result = grouped.sum(min_count=1)
    # Any undefined source estimate makes the target undefined.
    nan_any = pd.DataFrame(np.isnan(est), columns=est_cols, index=merged.index).groupby(
        [merged[k] for k in keys], sort=True
    ).any()
    result = result.mask(nan_any)

    for e, m in moe_pairs:
        wm = merged[m].to_numpy(dtype=float) * merged["weight"].to_numpy()
        e_vals = merged[e].to_numpy(dtype=float)
        g = [merged[k] for k in keys]
        if zero_estimate_rule == "largest":
            zero = e_vals == 0
            sq = pd.Series(np.where(zero, 0.0, wm**2), index=merged.index).groupby(g, sort=True).sum()
            zmax = pd.Series(np.where(zero, wm, 0.0), index=merged.index).groupby(g, sort=True).max()
            var = sq + zmax**2
        else:
            var = pd.Series(wm**2, index=merged.index).groupby(g, sort=True).sum()
        undefined = pd.Series(np.isnan(wm), index=merged.index).groupby(g, sort=True).any()
        result[m] = np.sqrt(var.where(~undefined)).values

    result = result.reset_index().rename(columns={"target_geoid": geoid_col})

    # Coverage: share of each target's mass (part_area, else weight) present in the input.
    full = crosswalk.groupby("target_geoid")[mass_col].sum()
    have = merged.groupby(keys, sort=True)[mass_col].sum().reset_index()
    have["full"] = have["target_geoid"].map(full).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        have["coverage"] = np.where(have["full"] > 0, have[mass_col] / have["full"], 1.0)
    have = have.rename(columns={"target_geoid": geoid_col})[list(group_cols) + [geoid_col, "coverage"]]
    return result, have


def apportion(
    df: pd.DataFrame,
    crosswalk: pd.DataFrame,
    estimate_cols: Optional[Sequence[str]] = None,
    geoid_col: str = "GEOID",
    moe_cols: Optional[Mapping[str, str]] = None,
    metadata: Optional[Mapping[str, Mapping[str, str]]] = None,
    assume_additive: Union[bool, Sequence[str]] = False,
    non_additive: str = "raise",
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    zero_estimate_rule: str = "all",
    group_cols: Sequence[str] = (),
) -> pd.DataFrame:
    """Apportion additive ACS estimates from source to target geography.

    ``df`` is a wide ACS frame (one row per source GEOID, or per GEOID x
    ``group_cols`` such as ``Year``) with ``<base>E`` / ``<base>M`` columns.
    ``estimate_cols`` defaults to every ``E`` column; MOEs are paired by code
    (``B01003_001E`` -> ``B01003_001M``) or via ``moe_cols``.

    Additivity: columns are checked against labels in ``metadata`` (or the
    offline detailed-table archive when None). Non-additive columns raise
    ValueError unless ``non_additive="drop"``; columns with no label raise
    unless ``assume_additive`` is True or lists them.

    Returns one row per target GEOID (x ``group_cols``) with apportioned E and
    M columns. Targets whose land area is covered by the input's source tracts
    less than ``min_coverage`` (e.g. a target straddling a county the input
    does not include) are dropped. ``attrs``: ``method`` ("apportioned"),
    ``weight_type``, ``notes``, ``limitations``, ``dropped_columns``,
    ``unmatched_geoids``, ``incomplete_targets``.
    """
    if non_additive not in NON_ADDITIVE_POLICIES:
        raise ValueError(f"non_additive must be one of {NON_ADDITIVE_POLICIES}")
    if zero_estimate_rule not in ZERO_RULES:
        raise ValueError(f"zero_estimate_rule must be one of {ZERO_RULES}")
    missing = [c for c in CROSSWALK_COLUMNS if c not in crosswalk.columns]
    if missing:
        raise ValueError(f"crosswalk lacks column(s) {missing}")
    if geoid_col not in df.columns:
        raise KeyError(f"df has no {geoid_col!r} column")

    cols = [str(c) for c in df.columns]
    explicit = estimate_cols is not None
    if explicit:
        candidates = list(estimate_cols)  # type: ignore[arg-type]
        absent = [c for c in candidates if c not in cols]
        if absent:
            raise KeyError(f"estimate column(s) not in df: {absent}")
    else:
        candidates = [c for c in cols if (parse_variable_code(c) or ("", ""))[1] in ("E", "PE")]
        if not candidates:
            raise ValueError("no estimate (E) columns found; pass estimate_cols=")

    ok, refused, unverified = check_additive(candidates, metadata, assume_additive)
    dropped: Dict[str, str] = {}
    if refused:
        if non_additive == "raise":
            raise ValueError(_refusal_message(refused))
        dropped.update(refused)
    if unverified:
        raise ValueError(
            f"Cannot verify that {len(unverified)} column(s) are additive counts (no label "
            f"metadata): {unverified[:8]}. Pass metadata= (code -> {{'label': ...}}), or "
            "assume_additive=True / a list of columns after checking they are counts."
        )
    if not ok:
        raise ValueError("no additive estimate columns left to apportion")
    pairs = [(e, _moe_column(e, cols, moe_cols)) for e in ok]

    work = df.copy()
    work[geoid_col] = normalize_tract_geoids(work[geoid_col]).values if _looks_like_tracts(crosswalk) else work[geoid_col].astype(str)
    dup = work.duplicated(subset=list(group_cols) + [geoid_col])
    if dup.any():
        raise ValueError(f"{int(dup.sum())} duplicate {geoid_col} row(s) in df")
    sources = set(crosswalk["source_geoid"])
    unmatched = sorted(set(work[geoid_col]) - sources)

    result, coverage = _apportion_frame(work, crosswalk, pairs, geoid_col, group_cols, zero_estimate_rule)
    result = result.merge(coverage, on=list(group_cols) + [geoid_col], how="left")
    low = result["coverage"] < min_coverage
    incomplete = sorted(result.loc[low, geoid_col].unique())
    result = result.loc[~low].drop(columns="coverage").reset_index(drop=True)

    weight_type = crosswalk.attrs.get("weight_type", "user-supplied")
    notes = [
        f"apportioned with {weight_type} weights; estimate = sum(w * X)",
        NOTE_INDEPENDENCE,
    ] + [n for n in crosswalk.attrs.get("notes", []) if n not in (NOTE_INDEPENDENCE,)]
    if zero_estimate_rule == "largest":
        notes.append("zero-estimate components contribute only their largest weighted MOE")
    if unmatched:
        notes.append(f"{len(unmatched)} source GEOID(s) not in the crosswalk were left out")
    if incomplete:
        notes.append(
            f"{len(incomplete)} target(s) dropped: input covers < {min_coverage:.0%} of their area"
        )
    result.attrs.update(
        method=METHOD_APPORTIONED,
        weight_type=weight_type,
        notes=notes,
        limitations=list(LIMITATIONS),
        dropped_columns=dropped,
        unmatched_geoids=unmatched,
        incomplete_targets=incomplete,
    )
    return result


def _looks_like_tracts(crosswalk: pd.DataFrame) -> bool:
    sample = crosswalk["source_geoid"].head(50).astype(str)
    return bool(len(sample)) and bool(sample.str.fullmatch(r"\d{11}").all())


def apportion_long(
    df: pd.DataFrame,
    crosswalk: pd.DataFrame,
    geoid_col: str = "GEOID",
    variable_col: str = "variable",
    group_cols: Sequence[str] = (),
    **kwargs: Any,
) -> pd.DataFrame:
    """Apportion a long frame (``GEOID, [group_cols], variable, estimate, moe``).

    Returns ``GEOID, [group_cols], variable, estimate, moe, se, cv_percent,
    method, notes`` (the ``census.variance`` result fields).
    """
    for col in (geoid_col, variable_col, "estimate", "moe"):
        if col not in df.columns:
            raise KeyError(f"long frame needs a {col!r} column")
    idx = list(group_cols) + [geoid_col]
    variables = [str(v) for v in pd.unique(df[variable_col])]
    est = df.pivot_table(index=idx, columns=variable_col, values="estimate", aggfunc="first", dropna=False)
    moe = df.pivot_table(index=idx, columns=variable_col, values="moe", aggfunc="first", dropna=False)
    est.columns = [f"{c}::E" for c in est.columns]
    moe.columns = [f"{c}::M" for c in moe.columns]
    wide = pd.concat([est, moe], axis=1).reset_index()
    names = {f"{v}::E": v for v in variables}
    meta = kwargs.pop("metadata", None)
    assume = kwargs.pop("assume_additive", False)
    ok, refused, unverified = check_additive(variables, meta, assume)
    if refused and kwargs.get("non_additive", "raise") == "raise":
        raise ValueError(_refusal_message(refused))
    if unverified:
        raise ValueError(
            f"Cannot verify that variable(s) {unverified[:8]} are additive counts; pass "
            "metadata= or assume_additive="
        )
    est_cols = [f"{v}::E" for v in ok]
    out = apportion(
        wide, crosswalk, estimate_cols=est_cols, geoid_col=geoid_col,
        moe_cols={f"{v}::E": f"{v}::M" for v in ok}, assume_additive=True,
        group_cols=group_cols, **kwargs,
    )
    records = []
    for col in est_cols:
        part = out[list(group_cols) + [geoid_col]].copy()
        part[variable_col] = names[col]
        part["estimate"] = out[col].values
        part["moe"] = out[col.replace("::E", "::M")].values
        records.append(part)
    long = pd.concat(records, ignore_index=True)
    long["se"] = long["moe"] / ACS_MOE_Z
    with np.errstate(invalid="ignore", divide="ignore"):
        long["cv_percent"] = np.where(long["estimate"] != 0, long["se"] / long["estimate"].abs() * 100, np.nan)
    long["method"] = METHOD_APPORTIONED
    long["notes"] = f"{out.attrs['weight_type']} weights; {NOTE_INDEPENDENCE}"
    long.attrs.update(out.attrs)
    long.attrs["dropped_columns"] = refused if refused else {}
    return long


# ---------------------------------------------------------------------------
# Panel harmonization
# ---------------------------------------------------------------------------

_DERIVED_SUFFIX_RE = re.compile(r"^(?P<key>.+?)_(?P<kind>SE|CV|RELIABILITY)$")


def _recompute_reliability(panel: pd.DataFrame, rows: pd.Index) -> None:
    for col in list(panel.columns):
        m = _DERIVED_SUFFIX_RE.match(str(col))
        if not m:
            continue
        key, kind = m.group("key"), m.group("kind")
        # Keys are "<base>" for E/M pairs and "<base>P" for PE/PM pairs (census.stats).
        if key.endswith("P") and f"{key[:-1]}PE" in panel.columns:
            est_col, moe_col = f"{key[:-1]}PE", f"{key[:-1]}PM"
        else:
            est_col, moe_col = f"{key}E", f"{key}M"
        if est_col not in panel.columns or moe_col not in panel.columns:
            continue
        est = pd.to_numeric(panel.loc[rows, est_col], errors="coerce")
        se = pd.to_numeric(panel.loc[rows, moe_col], errors="coerce").abs() / ACS_MOE_Z
        cv = se / est.abs().where(est != 0) * 100.0
        if kind == "SE":
            panel.loc[rows, col] = se
        elif kind == "CV":
            panel.loc[rows, col] = cv
        else:
            panel.loc[rows, col] = cv.map(reliability_flag)


def harmonize_panel(
    df: pd.DataFrame,
    target_vintage: int = 2020,
    weight: str = WEIGHT_LAND,
    weights_file: Optional[str] = None,
    crosswalk: Optional[pd.DataFrame] = None,
    geoid_col: str = "GEOID",
    year_col: str = "Year",
    metadata: Optional[Mapping[str, Mapping[str, str]]] = None,
    assume_additive: Union[bool, Sequence[str]] = False,
    non_additive: str = "raise",
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    zero_estimate_rule: str = "all",
    ct_coding: str = "auto",
    return_record: bool = False,
) -> Union[pd.DataFrame, Tuple[pd.DataFrame, Dict[str, Any]]]:
    """Put a multi-release tract panel on one tract vintage.

    Rows from releases already on ``target_vintage`` tracts are kept (GEOIDs
    recoded to one consistent coding); rows from releases on the other vintage
    are apportioned with :func:`apportion`, one release at a time. Accepts the
    wide (GEOID, Year) panel from ``census.panel.build_tidy_panel`` (raw code
    columns) or a long frame with ``variable, estimate, moe`` columns.

    Wide panels: ``_SE``/``_CV``/``_RELIABILITY`` columns are recomputed for
    apportioned rows; ``_NOTE`` and annotation columns are blanked there. With
    ``non_additive="drop"``, non-additive columns (``PE``/``PM``, medians, ...)
    are kept for native rows and set to NaN for apportioned rows.

    GEOID coding: target 2010 uses the codes of the latest 2010-vintage ACS
    release (e.g. Oglala Lakota 46102); target 2020 uses Connecticut planning-
    region codes when ``ct_coding`` is ``"2022"`` (``"auto"``: when the panel
    has a 2022+ release) and the former county codes when ``"2020"``.

    The returned frame's ``attrs["harmonization"]`` (and the second element
    when ``return_record=True``) is a JSON-ready record for run metadata.
    """
    if target_vintage not in (2010, 2020):
        raise ValueError("target_vintage must be 2010 or 2020")
    for col in (geoid_col, year_col):
        if col not in df.columns:
            raise KeyError(f"panel needs a {col!r} column")
    long_format = {"variable", "estimate", "moe"}.issubset(df.columns)
    direction = "2010_to_2020" if target_vintage == 2020 else "2020_to_2010"
    years = sorted(int(y) for y in pd.unique(df[year_col]))
    vintages = {y: tract_vintage(y) for y in years}
    if ct_coding == "auto":
        ct_coding = "2022" if any(y >= FIRST_RELEASE_CT_PLANNING_REGIONS for y in years) else "2020"
    if ct_coding not in ("2020", "2022"):
        raise ValueError("ct_coding must be 'auto', '2020', or '2022'")

    if crosswalk is None:
        crosswalk = tract_crosswalk(direction, weight=weight, weights_file=weights_file)
    cw = crosswalk.copy()
    cw.attrs = dict(crosswalk.attrs)

    panel = df.copy()
    panel[geoid_col] = normalize_tract_geoids(panel[geoid_col]).values
    panel[year_col] = pd.to_numeric(panel[year_col]).astype("int64")
    recodes: Dict[str, int] = {}

    def _count(label: str, before: pd.Series, after: pd.Series) -> None:
        n = int((before.values != after.values).sum())
        if n:
            recodes[label] = recodes.get(label, 0) + n

    # 1. Bring source rows onto the crosswalk's (decennial Census) codes.
    src_years = [y for y in years if vintages[y] != target_vintage]
    native_years = [y for y in years if vintages[y] == target_vintage]
    src_mask = panel[year_col].isin(src_years)
    before = panel.loc[src_mask, geoid_col]
    if target_vintage == 2020:
        after = before.map(acs_to_census2010_geoid)
        _count("acs_2010_codes_to_census_2010", before, after)
    else:
        after = recode_ct_tracts(before, to="2020")
        _count("ct_planning_region_to_2020_county", before, after)
    panel.loc[src_mask, geoid_col] = after.values

    # 2. Bring native rows and crosswalk targets onto one output coding.
    nat_mask = panel[year_col].isin(native_years)
    before = panel.loc[nat_mask, geoid_col]
    if target_vintage == 2010:
        after = before.map(acs_to_census2010_geoid).map(census2010_to_acs_geoid)
        cw["target_geoid"] = cw["target_geoid"].map(census2010_to_acs_geoid)
        _count("acs_2010_codes_to_latest", before, after)
    else:
        after = recode_ct_tracts(before, to=ct_coding)
        cw["target_geoid"] = recode_ct_tracts(cw["target_geoid"], to=ct_coding).values
        _count(f"ct_to_{ct_coding}_coding", before, after)
    panel.loc[nat_mask, geoid_col] = after.values

    # Compare normalized GEOIDs: a panel read without dtype=str loses leading zeros.
    boundary_hits = sorted(
        set(normalize_tract_geoids(df[geoid_col]).astype(str)) & set(ACS_2010_BOUNDARY_CHANGES)
    )
    record: Dict[str, Any] = {
        "method": METHOD_APPORTIONED,
        "target_vintage": target_vintage,
        "direction": direction,
        "weight_type": cw.attrs.get("weight_type", "user-supplied"),
        "weights_file": os.path.basename(weights_file) if weights_file else None,
        "relationship_file": relationship_source() if weights_file is None else None,
        "years_native": native_years,
        "years_apportioned": src_years,
        "connecticut_coding": ct_coding if target_vintage == 2020 else "2020 county-based",
        "geoid_recodes": recodes,
        "min_coverage": min_coverage,
        "zero_estimate_rule": zero_estimate_rule,
        "rows_in": int(len(df)),
        "unmatched_geoids": {},
        "incomplete_targets": {},
        "dropped_columns": {},
        "notes": list(cw.attrs.get("notes", [])),
        "limitations": list(LIMITATIONS),
    }
    if boundary_hits:
        record["notes"].append(
            "GEOIDs with post-2010 boundary changes cannot be matched exactly: "
            + "; ".join(f"{g} ({ACS_2010_BOUNDARY_CHANGES[g]})" for g in boundary_hits)
        )

    if not src_years:
        out = panel
    else:
        native = panel.loc[nat_mask]
        parts: List[pd.DataFrame] = [native]
        kwargs = dict(
            metadata=metadata, assume_additive=assume_additive, non_additive=non_additive,
            min_coverage=min_coverage, zero_estimate_rule=zero_estimate_rule,
            geoid_col=geoid_col,
        )
        for year in src_years:
            rows = panel.loc[panel[year_col] == year]
            if long_format:
                res = apportion_long(rows, cw, group_cols=[year_col], **kwargs)
            else:
                res = _apportion_wide_panel_rows(rows, cw, year_col, kwargs)
            record["unmatched_geoids"][str(year)] = res.attrs.get("unmatched_geoids", [])
            record["incomplete_targets"][str(year)] = len(res.attrs.get("incomplete_targets", []))
            record["dropped_columns"].update(res.attrs.get("dropped_columns", {}))
            parts.append(res)
        out = pd.concat(parts, ignore_index=True, sort=False)
        if not long_format:
            out = out[[c for c in panel.columns if c in out.columns]]
            apportioned = out.index[out[year_col].isin(src_years)]
            _recompute_reliability(out, apportioned)
        sort_cols = [geoid_col, year_col] + (["variable"] if long_format else [])
        out = out.sort_values(sort_cols).reset_index(drop=True)
        if not long_format:
            out = _restore_dtypes(out, panel)
    record["rows_out"] = int(len(out))
    record["unmatched_geoids"] = {k: v for k, v in record["unmatched_geoids"].items() if v}
    out.attrs["harmonization"] = record
    return (out, record) if return_record else out


def _apportion_wide_panel_rows(
    rows: pd.DataFrame, cw: pd.DataFrame, year_col: str, kwargs: Dict[str, Any]
) -> pd.DataFrame:
    res = apportion(rows, cw, group_cols=[year_col], **kwargs)
    # Non-value columns (NAME, notes, annotations, derived) are blank for apportioned rows.
    for col in rows.columns:
        if col in res.columns:
            continue
        info = parse_variable_code(str(col))
        if info is not None and info[1] in ("E", "M", "PE", "PM"):
            res[col] = np.nan
        elif _DERIVED_SUFFIX_RE.match(str(col)) and not str(col).endswith("_RELIABILITY"):
            res[col] = np.nan
        else:
            res[col] = ""
    return res


def _restore_dtypes(out: pd.DataFrame, like: pd.DataFrame) -> pd.DataFrame:
    for col in out.columns:
        if col in like.columns and pd.api.types.is_numeric_dtype(like[col]) and not pd.api.types.is_numeric_dtype(out[col]):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        elif col in like.columns and like[col].dtype == object:
            out[col] = out[col].fillna("") if not pd.api.types.is_numeric_dtype(out[col]) else out[col]
    return out


__all__ = [
    "ACS_2010_BOUNDARY_CHANGES",
    "ACS_2010_COUNTY_RECODES",
    "ACS_2010_TRACT_RENUMBERING",
    "acs_to_census2010_geoid",
    "apportion",
    "apportion_long",
    "census2010_to_acs_geoid",
    "check_additive",
    "harmonize_panel",
    "load_ct_tract_recode",
    "load_tract_relationships",
    "load_weight_file",
    "normalize_tract_geoid",
    "normalize_tract_geoids",
    "recode_ct_tracts",
    "relationship_source",
    "tract_crosswalk",
    "tract_vintage",
]
