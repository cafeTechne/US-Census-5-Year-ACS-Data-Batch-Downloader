# SPDX-License-Identifier: AGPL-3.0-or-later
"""Population- and housing-unit-weighted 2010 <-> 2020 tract crosswalks from census blocks.

Sources (verified November 2026)
--------------------------------
* **Block relationship files**, one zip per state:
  ``https://www2.census.gov/geo/docs/maps-data/data/rel2020/t10t20/TAB2010_TAB2020_ST<SS>.zip``
  (pipe-delimited ``tab2010_tab2020_st<ss>_<abbr>.txt``; one row per
  intersection of a 2010 and a 2020 tabulation block with ``AREALAND_INT`` and
  ``AREAWATER_INT`` in square meters; 52 files: 50 states, DC, Puerto Rico).
* **Block counts**, Census API, P.L. 94-171 redistricting data (``dec/pl``):
  2010 ``P001001`` (total population) and ``H001001`` (housing units), one
  request per county (``for=block:*&in=state:SS county:CCC tract:*``; the 2010
  endpoint returns no rows for ``county:*``); 2020 ``P1_001N`` and ``H1_001N``,
  one request per state.

Files are cached under ``$CENSUS_BLOCK_DIR`` or ``<repo>/.cache/blocks`` with a
JSON sidecar (source URL without the API key, retrieval time, SHA-256);
``CENSUS_BLOCK_OFFLINE=1`` uses the cache only.

Method
------
For direction 2010 -> 2020, each 2010 block's count (population or housing
units) is split across its intersections with 2020 blocks in proportion to
intersection land area (total area for water-only blocks, an equal split when
a block has no area). Masses are summed to (2010 tract, 2020 tract) pairs and

    w(s, t) = mass(s, t) / sum_t' mass(s, t')

so the weights of each source tract sum to 1. Source tracts with no population
(or housing units) fall back to land-area weights from the same block pieces;
the number is reported in ``attrs["fallbacks"]``. Direction 2020 -> 2010 uses
2020 block counts. Within a split block, people are assumed evenly spread over
land (the remaining approximation; most blocks are not split).

The same block files do not identify ZCTAs or places; those crosswalks need
the block assignment files and are not built here.
"""

import io
import json
import os
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .api import BASE_URL, DEFAULT_API_KEY
from .replicate import USER_AGENT, _read_cached, _sha256, _sidecar_path, _write_cached
from .geo import get_resource_path

REL_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/t10t20/TAB2010_TAB2020_ST{state}.zip"
# year -> (dataset, population variable, housing-unit variable)
DECENNIAL_COUNTS = {
    2010: ("dec/pl", "P001001", "H001001"),
    2020: ("dec/pl", "P1_001N", "H1_001N"),
}
WEIGHT_POPULATION = "population"
WEIGHT_HOUSING = "housing_units"
WEIGHT_LAND = "land_area"
BLOCK_WEIGHTS = (WEIGHT_POPULATION, WEIGHT_HOUSING)
COUNT_COLUMN = {WEIGHT_POPULATION: "population", WEIGHT_HOUSING: "housing_units"}
DIRECTIONS = ("2010_to_2020", "2020_to_2010")
# 50 states, DC, Puerto Rico (the relationship files cover exactly these).
STATES = tuple(
    "01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 "
    "31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56 72".split()
)
NOTE_BLOCK = (
    "{weight} weights from 2010/2020 Census block counts (dec/pl) and the Census 2010-2020 "
    "block relationship files; split blocks are divided by land area"
)
# Detailed-table prefixes whose universe is households or housing units.
_HOUSING_TABLE_PREFIXES = ("B25", "B11", "B19001", "B19019", "B19025", "B19049", "B19050",
                           "B19051", "B19052", "B19053", "B19054", "B19055", "B19056", "B19057",
                           "B19058", "B19059", "B19060", "B19061", "B19062", "B19063", "B19064",
                           "B19065", "B19066", "B19067", "B19069", "B19070", "B19071", "B19081",
                           "B22", "B28", "B08201", "B09019", "B09005", "B11001", "B17017",
                           "B992", "C25")


class BlockDataError(RuntimeError):
    """Block relationship or count data could not be fetched or parsed."""


def block_cache_dir() -> str:
    """$CENSUS_BLOCK_DIR or <repo>/.cache/blocks."""
    return os.getenv("CENSUS_BLOCK_DIR") or get_resource_path(os.path.join(".cache", "blocks"))


def _offline() -> bool:
    return os.getenv("CENSUS_BLOCK_OFFLINE", "").strip() not in ("", "0", "false", "False")


def weight_for_variable(code: str) -> str:
    """``housing_units`` for household / housing-unit tables, else ``population``."""
    table = str(code).split("_", 1)[0].upper()
    return WEIGHT_HOUSING if table.startswith(_HOUSING_TABLE_PREFIXES) else WEIGHT_POPULATION


def _state(state: Any) -> str:
    code = str(state).strip().zfill(2)
    if code not in STATES:
        raise ValueError(f"no block relationship file for state FIPS {state!r}")
    return code


def _get(url: str, params: Optional[Dict[str, str]] = None, session: Any = None, timeout: float = 300.0) -> bytes:
    import requests

    getter = session if session is not None else requests
    try:
        resp = getter.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        raise BlockDataError(f"download failed for {url}: {exc}") from exc
    if resp.status_code != 200:
        raise BlockDataError(f"HTTP {resp.status_code} for {url}")
    raw = resp.content or b""
    if b"Request Rejected" in raw[:2000]:
        raise BlockDataError(f"census.gov rejected the request for {url}; retry later")
    return raw


def _cached_or_fetch(path: str, url: str, fetch: Any, content_type: str) -> bytes:
    raw = _read_cached(path)
    if raw is not None:
        return raw
    if _offline():
        raise BlockDataError(f"{os.path.basename(path)} not cached and CENSUS_BLOCK_OFFLINE is set")
    raw = fetch()
    _write_cached(path, raw, url, content_type)
    return raw


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def parse_block_relationships(text: str) -> pd.DataFrame:
    """Parse a ``tab2010_tab2020_st<ss>`` file into block / tract pieces.

    Columns: ``block_2010, block_2020`` (15-digit GEOIDs), ``tract_2010,
    tract_2020`` (11), ``arealand_int, areawater_int`` (square meters).
    """
    df = pd.read_csv(io.StringIO(text.lstrip("﻿")), sep="|", dtype=str, keep_default_na=False)
    need = ["STATE_2010", "COUNTY_2010", "TRACT_2010", "BLK_2010", "STATE_2020", "COUNTY_2020",
            "TRACT_2020", "BLK_2020", "AREALAND_INT", "AREAWATER_INT"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise BlockDataError(f"block relationship file lacks column(s) {missing}")
    df = df[(df["BLK_2010"] != "") & (df["BLK_2020"] != "")]
    t10 = df["STATE_2010"] + df["COUNTY_2010"] + df["TRACT_2010"]
    t20 = df["STATE_2020"] + df["COUNTY_2020"] + df["TRACT_2020"]
    return pd.DataFrame({
        "block_2010": (t10 + df["BLK_2010"]).values,
        "block_2020": (t20 + df["BLK_2020"]).values,
        "tract_2010": t10.values,
        "tract_2020": t20.values,
        "arealand_int": pd.to_numeric(df["AREALAND_INT"], errors="coerce").fillna(0).astype(float).values,
        "areawater_int": pd.to_numeric(df["AREAWATER_INT"], errors="coerce").fillna(0).astype(float).values,
    })


def load_block_relationships(state: Any, session: Any = None) -> pd.DataFrame:
    """2010/2020 block intersections for one state (downloaded once, cached)."""
    st = _state(state)
    url = REL_URL.format(state=st)
    path = os.path.join(block_cache_dir(), os.path.basename(url))
    raw = _cached_or_fetch(path, url, lambda: _get(url, session=session), "application/zip")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
            text = zf.read(name).decode("utf-8-sig")
    except (zipfile.BadZipFile, StopIteration) as exc:
        raise BlockDataError(f"{url}: not a block relationship zip") from exc
    return parse_block_relationships(text)


def _rows_to_counts(payload: Any, pop_var: str, hu_var: str) -> pd.DataFrame:
    if not isinstance(payload, list) or not payload:
        return pd.DataFrame(columns=["block_geoid", "population", "housing_units"])
    df = pd.DataFrame(payload[1:], columns=payload[0])
    geoid = df["state"].str.zfill(2) + df["county"].str.zfill(3) + df["tract"].str.zfill(6) + df["block"].str.zfill(4)
    return pd.DataFrame({
        "block_geoid": geoid.values,
        "population": pd.to_numeric(df[pop_var], errors="coerce").values,
        "housing_units": pd.to_numeric(df[hu_var], errors="coerce").values,
    })


def load_block_counts(
    state: Any,
    year: int,
    counties: Optional[Iterable[str]] = None,
    api_key: Optional[str] = None,
    session: Any = None,
) -> pd.DataFrame:
    """Decennial block population and housing units for one state (cached CSV).

    ``year`` 2010 needs the state's county codes (one API request per county);
    when not given they are taken from the block relationship file.
    """
    if year not in DECENNIAL_COUNTS:
        raise ValueError(f"year must be one of {sorted(DECENNIAL_COUNTS)}")
    st = _state(state)
    dataset, pop_var, hu_var = DECENNIAL_COUNTS[year]
    endpoint = f"{BASE_URL}/{year}/{dataset}"
    path = os.path.join(block_cache_dir(), f"dec{year}_pl_blocks_{st}.csv")
    key = api_key if api_key is not None else DEFAULT_API_KEY
    public_url = f"{endpoint}?get={pop_var},{hu_var}&for=block:*&in=state:{st}" + (
        "+county:<each>+tract:*" if year == 2010 else "+county:*"
    )

    def fetch() -> bytes:
        def call(inn: str) -> pd.DataFrame:
            params = {"get": f"{pop_var},{hu_var}", "for": "block:*", "in": inn}
            if key:
                params["key"] = key
            raw = _get(endpoint, params=params, session=session, timeout=120)
            try:
                return _rows_to_counts(json.loads(raw.decode("utf-8")), pop_var, hu_var)
            except (ValueError, KeyError) as exc:
                raise BlockDataError(f"unexpected API response for {endpoint} in={inn}") from exc

        if year == 2010:
            cos = sorted(set(counties)) if counties is not None else sorted(
                load_block_relationships(st, session=session)["tract_2010"].str[2:5].unique()
            )
            frames = [call(f"state:{st} county:{c} tract:*") for c in cos]
        else:
            frames = [call(f"state:{st} county:*")]
        out = pd.concat(frames, ignore_index=True)
        if out.empty:
            raise BlockDataError(f"no block counts returned for state {st}, {year}")
        return out.to_csv(index=False, lineterminator="\n").encode("utf-8")

    raw = _cached_or_fetch(path, public_url, fetch, "text/csv")
    df = pd.read_csv(io.BytesIO(raw), dtype={"block_geoid": str})
    df["block_geoid"] = df["block_geoid"].str.zfill(15)
    return df


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------


def _share_within(groups: pd.Series, land: np.ndarray, total: np.ndarray) -> np.ndarray:
    """Share of each piece within its group: land, else total area, else equal."""
    land_sum = pd.Series(land).groupby(groups.values).transform("sum").to_numpy()
    total_sum = pd.Series(total).groupby(groups.values).transform("sum").to_numpy()
    n = pd.Series(land).groupby(groups.values).transform("size").to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(land_sum > 0, land / land_sum, np.where(total_sum > 0, total / total_sum, 1.0 / n))


def block_tract_weights(
    relationships: pd.DataFrame,
    counts: Optional[pd.DataFrame],
    direction: str = "2010_to_2020",
    weight: str = WEIGHT_POPULATION,
) -> pd.DataFrame:
    """Tract crosswalk ``source_geoid, target_geoid, weight, part_area, mass``.

    ``relationships`` from :func:`parse_block_relationships`; ``counts`` from
    :func:`load_block_counts` for the *source* vintage (ignored for
    ``weight="land_area"``). ``part_area`` is the land (else total) area of
    each piece, used by ``apportion`` for coverage.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if weight not in BLOCK_WEIGHTS + (WEIGHT_LAND,):
        raise ValueError(f"weight must be one of {BLOCK_WEIGHTS + (WEIGHT_LAND,)}")
    sv, tv = ("2010", "2020") if direction == "2010_to_2020" else ("2020", "2010")
    rel = relationships
    land = rel["arealand_int"].to_numpy(dtype=float)
    total = land + rel["areawater_int"].to_numpy(dtype=float)
    missing_blocks = 0
    if weight == WEIGHT_LAND:
        mass = np.zeros(len(rel))
    else:
        if counts is None:
            raise ValueError("counts are required for block-count weights")
        lookup = pd.Series(counts[COUNT_COLUMN[weight]].to_numpy(dtype=float), index=counts["block_geoid"].to_numpy())
        lookup = lookup[~lookup.index.duplicated()]
        block_count = rel[f"block_{sv}"].map(lookup)
        missing_blocks = int(rel.loc[block_count.isna(), f"block_{sv}"].nunique())
        share = _share_within(rel[f"block_{sv}"], land, total)
        mass = block_count.fillna(0).to_numpy() * share
    pieces = pd.DataFrame({
        "source_geoid": rel[f"tract_{sv}"].to_numpy(),
        "target_geoid": rel[f"tract_{tv}"].to_numpy(),
        "mass": mass, "land": land, "total": total,
    })
    pairs = pieces.groupby(["source_geoid", "target_geoid"], as_index=False, sort=True)[["mass", "land", "total"]].sum()
    g = pairs.groupby("source_geoid")
    msum = g["mass"].transform("sum").to_numpy()
    lsum = g["land"].transform("sum").to_numpy()
    tsum = g["total"].transform("sum").to_numpy()
    n = g["mass"].transform("size").to_numpy()
    use_mass = msum > 0
    use_land = ~use_mass & (lsum > 0)
    use_total = ~use_mass & ~use_land & (tsum > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(use_mass, pairs["mass"] / msum,
                     np.where(use_land, pairs["land"] / lsum,
                              np.where(use_total, pairs["total"] / tsum, 1.0 / n)))
    pairs["weight"] = w
    pairs["part_area"] = np.where(pairs["land"] > 0, pairs["land"], pairs["total"])
    out = pairs.loc[pairs["weight"] > 0, ["source_geoid", "target_geoid", "weight", "part_area", "mass"]]
    out = out.reset_index(drop=True)
    src = pairs["source_geoid"]
    fallbacks = {
        "no_count_land_area": int(src[use_land].nunique()) if weight != WEIGHT_LAND else 0,
        "total_area": int(src[use_total].nunique()),
        "equal_split": int(src[~use_mass & ~use_land & ~use_total].nunique()),
        "blocks_without_counts": missing_blocks,
    }
    notes = [NOTE_BLOCK.format(weight=weight) if weight != WEIGHT_LAND else
             "land_area weights from Census 2010-2020 block relationship pieces"]
    if fallbacks["no_count_land_area"]:
        notes.append(f"{fallbacks['no_count_land_area']} source tract(s) with no {COUNT_COLUMN[weight]} use land-area weights")
    if missing_blocks:
        notes.append(f"{missing_blocks} source block(s) had no decennial count and were treated as 0")
    out.attrs.update(weight_type=weight, direction=direction, fallbacks=fallbacks, notes=notes)
    return out


def tract_weights(
    states: Optional[Sequence[Any]] = None,
    direction: str = "2010_to_2020",
    weight: str = WEIGHT_POPULATION,
    api_key: Optional[str] = None,
    session: Any = None,
    on_error: str = "raise",
) -> pd.DataFrame:
    """Block-based tract crosswalk for ``states`` (default: all 52).

    ``on_error="skip"`` leaves out states whose data cannot be fetched and lists
    them in ``attrs["failed_states"]`` (with reasons); ``"raise"`` re-raises.
    """
    sv = 2010 if direction == "2010_to_2020" else 2020
    frames: List[pd.DataFrame] = []
    failed: Dict[str, str] = {}
    fallbacks: Dict[str, int] = {}
    notes: List[str] = []
    for st in [_state(s) for s in (states or STATES)]:
        try:
            rel = load_block_relationships(st, session=session)
            # A state's file also lists pieces whose source block lies in a
            # neighbouring state (boundary changes); those belong to that
            # state's run, so keep only this state's source tracts (each piece
            # is counted once) and dedupe repeated block pairs.
            rel = rel[rel[f"tract_{sv}"].astype(str).str[:2] == st]
            rel = rel.drop_duplicates([f"block_{sv}", f"block_{2020 if sv == 2010 else 2010}"])
            counts = None if weight == WEIGHT_LAND else load_block_counts(
                st, sv, counties=sorted(rel[f"tract_{sv}"].astype(str).str[2:5].unique()), api_key=api_key,
                session=session
            )
            cw = block_tract_weights(rel, counts, direction, weight)
        except (BlockDataError, OSError, ValueError) as exc:
            if on_error == "raise":
                raise
            failed[st] = str(exc)
            continue
        for k, v in cw.attrs["fallbacks"].items():
            fallbacks[k] = fallbacks.get(k, 0) + v
        notes.extend(n for n in cw.attrs["notes"][1:])
        frames.append(cw)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["source_geoid", "target_geoid", "weight", "part_area", "mass"])
    # Each source tract is computed from its own state's file only, so no
    # (source, target) pair appears twice and weights are never re-normalized.
    out.attrs.update(
        weight_type=weight, direction=direction, fallbacks=fallbacks,
        notes=[NOTE_BLOCK.format(weight=weight)] + notes,
        failed_states=failed,
        source={"relationship_files": REL_URL, "counts": {y: f"{BASE_URL}/{y}/{d} {p},{h}"
                                                          for y, (d, p, h) in DECENNIAL_COUNTS.items()}},
    )
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def weight_differences(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """Per source tract: total-variation distance between two crosswalks (0-1)."""
    m = a[["source_geoid", "target_geoid", "weight"]].merge(
        b[["source_geoid", "target_geoid", "weight"]], on=["source_geoid", "target_geoid"],
        how="outer", suffixes=("_a", "_b")).fillna({"weight_a": 0.0, "weight_b": 0.0})
    m["absdiff"] = (m["weight_a"] - m["weight_b"]).abs()
    out = m.groupby("source_geoid").agg(tvd=("absdiff", "sum"), targets=("target_geoid", "size"))
    out["tvd"] = out["tvd"] / 2
    return out.reset_index().sort_values("tvd", ascending=False, ignore_index=True)


def _apply(values: pd.Series, cw: pd.DataFrame) -> pd.Series:
    m = cw.merge(values.rename("x"), left_on="source_geoid", right_index=True, how="inner")
    return (m["weight"] * m["x"]).groupby(m["target_geoid"]).sum()


def validate_weights(
    relationships: pd.DataFrame,
    counts_2010: pd.DataFrame,
    counts_2020: Optional[pd.DataFrame] = None,
    acs_tracts: Optional[pd.Series] = None,
    acs_counties: Optional[pd.Series] = None,
) -> Dict[str, Any]:
    """Check block weights (direction 2010 -> 2020) for one state's data.

    * weights of every source tract sum to 1 (population, housing units);
    * how much population weights differ from land-area weights (TVD);
    * ``acs_tracts`` (2010-vintage tract GEOID -> count, e.g. 2019 ACS
      B01003_001E) apportioned to 2020 tracts and summed by county must equal
      ``acs_counties`` (county GEOID -> count) for counties whose boundaries
      did not change;
    * with ``counts_2020``: error of apportioned 2010 Census tract population
      against 2020 Census tract population, for both weight types.
    """
    pop = block_tract_weights(relationships, counts_2010, weight=WEIGHT_POPULATION)
    hu = block_tract_weights(relationships, counts_2010, weight=WEIGHT_HOUSING)
    land = block_tract_weights(relationships, None, weight=WEIGHT_LAND)
    report: Dict[str, Any] = {}
    for name, cw in (("population", pop), ("housing_units", hu), ("land_area", land)):
        sums = cw.groupby("source_geoid")["weight"].sum()
        report[f"{name}_max_sum_error"] = float((sums - 1).abs().max())
    diff = weight_differences(pop, land)
    split = diff[diff["targets"] > 1]
    report["source_tracts"] = int(len(diff))
    report["split_source_tracts"] = int(len(split))
    report["tvd_pop_vs_land"] = {
        "mean_all": float(diff["tvd"].mean()),
        "mean_split": float(split["tvd"].mean()) if len(split) else 0.0,
        "p95_split": float(split["tvd"].quantile(0.95)) if len(split) else 0.0,
        "max": float(diff["tvd"].max()),
        "top": diff.head(5).to_dict("records"),
    }
    report["fallbacks"] = {"population": pop.attrs["fallbacks"], "housing_units": hu.attrs["fallbacks"]}
    if acs_tracts is not None and acs_counties is not None:
        checks = {}
        for name, cw in (("population", pop), ("land_area", land)):
            moved = _apply(acs_tracts, cw)
            by_county = moved.groupby(moved.index.str[:5]).sum()
            cmp = pd.DataFrame({"apportioned": by_county, "published": acs_counties}).dropna()
            cmp["diff"] = cmp["apportioned"] - cmp["published"]
            checks[name] = {
                "counties": int(len(cmp)),
                "max_abs_diff": float(cmp["diff"].abs().max()),
                "state_total_apportioned": float(moved.sum()),
                "state_total_published": float(acs_counties.sum()),
            }
        report["county_totals"] = checks
    if counts_2020 is not None:
        p10 = counts_2010.groupby(counts_2010["block_geoid"].str[:11])["population"].sum()
        p20 = counts_2020.groupby(counts_2020["block_geoid"].str[:11])["population"].sum()
        errs = {}
        for name, cw in (("population", pop), ("land_area", land)):
            est = _apply(p10, cw).reindex(p20.index).fillna(0)
            err = (est - p20).abs()
            errs[name] = {"mean_abs_error": float(err.mean()), "median_abs_error": float(err.median())}
        report["decennial_2010_to_2020_tract_error"] = errs
    return report


def validate_state(state: Any, acs_year: int = 2019, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Live validation for one state: downloads blocks, 2010/2020 counts and ACS B01003."""
    import requests

    from .geo_crosswalk import acs_to_census2010_geoid

    st = _state(state)
    rel = load_block_relationships(st)
    c10 = load_block_counts(st, 2010, api_key=api_key)
    c20 = load_block_counts(st, 2020, api_key=api_key)
    key = api_key if api_key is not None else DEFAULT_API_KEY

    def acs(geo: Dict[str, str]) -> pd.DataFrame:
        params = {"get": "B01003_001E", **geo}
        if key:
            params["key"] = key
        r = requests.get(f"{BASE_URL}/{acs_year}/acs/acs5", params=params, timeout=120)
        r.raise_for_status()
        j = r.json()
        return pd.DataFrame(j[1:], columns=j[0])

    t = acs({"for": "tract:*", "in": f"state:{st} county:*"})
    tracts = pd.Series(pd.to_numeric(t["B01003_001E"]).values,
                       index=[acs_to_census2010_geoid(s + c + tr) for s, c, tr in zip(t["state"], t["county"], t["tract"])])
    c = acs({"for": "county:*", "in": f"state:{st}"})
    counties = pd.Series(pd.to_numeric(c["B01003_001E"]).values, index=(c["state"] + c["county"]).values)
    report = validate_weights(rel, c10, c20, tracts, counties)
    report.update(state=st, acs_year=acs_year)
    return report


__all__ = [
    "BLOCK_WEIGHTS",
    "BlockDataError",
    "DECENNIAL_COUNTS",
    "REL_URL",
    "block_cache_dir",
    "block_tract_weights",
    "load_block_counts",
    "load_block_relationships",
    "parse_block_relationships",
    "tract_weights",
    "validate_state",
    "validate_weights",
    "weight_differences",
    "weight_for_variable",
]
