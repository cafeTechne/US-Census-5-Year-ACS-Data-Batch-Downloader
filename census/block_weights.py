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

ZCTA and place crosswalks (Round 10 F2)
---------------------------------------
The same block pieces are aggregated to (2010 area, 2020 area) pairs instead of
tract pairs, once each block is assigned to its area. Block assignment sources
(verified November 2026):

* **2020 blocks -> 2020 ZCTAs**: ``tab20_zcta520_tabblock20_natl.txt`` in
  ``https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/``
  (pipe-delimited, one row per 2020 tabulation block with ``GEOID_ZCTA5_20``
  blank for blocks in no ZCTA; national, about 1.06 GB, streamed once and
  cached as compact per-state files).
* **2010 blocks -> 2010 ZCTAs**: no 2010 block assignment file covers ZCTAs, so
  the ``ZCTA5CE10`` and ``BLOCKCE10`` fields of the TIGER/Line 2010 topological
  faces, ``https://www2.census.gov/geo/tiger/TIGER2010/FACES/tl_2010_<SSCCC>_faces.zip``
  (one per county; every face carries its 2010 tabulation block and ZCTA, and
  2010 ZCTAs are aggregations of 2010 blocks). Read with ``pyogrio``, no geometry.
* **Blocks -> places** (incorporated places and CDPs): the Census block
  assignment files, ``BlockAssign_ST<SS>_<AB>_INCPLACE_CDP.txt`` inside
  ``https://www2.census.gov/geo/docs/maps-data/data/baf/BlockAssign_ST<SS>_<AB>.zip``
  (2010, comma-delimited ``BLOCKID,PLACEFP``) and
  ``.../data/baf2020/BlockAssign_ST<SS>_<AB>.zip`` (2020, pipe-delimited).

Weights: ``w(s, t) = mass(s, t) / mass(s)`` where ``mass(s)`` includes mass in
blocks that fall in *no* target area, so a source's weights sum to less than 1
when part of its population moved outside every target (common for places:
annexations and new CDPs). ``target_coverage`` is the share of the block mass
landing in each target that came from some source area; below 1 the target
has population that was in no source area, and apportioned totals undercount
it. Sources with no mass fall back to land-area shares as for tracts. ZCTAs
cross state lines, so multi-state runs pool pieces before normalizing.
Place boundaries change every year (annexations), while these files fix them at
the 2010 and 2020 censuses; ACS place estimates for other vintages carry that
extra mismatch.
"""

import io
import json
import os
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .api import BASE_URL, DEFAULT_API_KEY
from .http import polite_get
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
ZCTA_2020_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_tabblock20_natl.txt"
FACES_2010_URL = "https://www2.census.gov/geo/tiger/TIGER2010/FACES/tl_2010_{state}{county}_faces.zip"
BAF_URLS = {
    2010: "https://www2.census.gov/geo/docs/maps-data/data/baf/BlockAssign_ST{state}_{abbr}.zip",
    2020: "https://www2.census.gov/geo/docs/maps-data/data/baf2020/BlockAssign_ST{state}_{abbr}.zip",
}
AREA_GEOGRAPHIES = ("zcta", "place")
# 50 states, DC, Puerto Rico (the relationship files cover exactly these).
STATES = tuple(
    "01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 "
    "31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56 72".split()
)
STATE_ABBR = dict(zip(STATES, (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split()
)))
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


def _get(url: str, params: Optional[Dict[str, str]] = None, session: Any = None, timeout: float = 300.0,
         cache_path: Optional[str] = None) -> bytes:
    resp = polite_get(url, user_agent=USER_AGENT, error=BlockDataError, session=session, timeout=timeout,
                      params=params, cache_path=cache_path)
    if resp.status_code != 200:
        raise BlockDataError(f"HTTP {resp.status_code} for {url}")
    return resp.content or b""


def _cached_or_fetch(path: str, url: str, fetch: Any, content_type: str, adopt: bool = False) -> bytes:
    """Cached bytes or ``fetch()``; with ``adopt``, a file placed at ``path`` by hand is used (raw downloads)."""
    raw = _read_cached(path, url if adopt else None)
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
    raw = _cached_or_fetch(path, url, lambda: _get(url, session=session, cache_path=path), "application/zip",
                           adopt=True)
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


def _block_pieces(
    rel: pd.DataFrame,
    counts: Optional[pd.DataFrame],
    direction: str,
    weight: str,
    source_area: Any,
    target_area: Any,
) -> "tuple[pd.DataFrame, int]":
    """Pieces ``source_geoid, target_geoid, mass, land, total`` and the number
    of source blocks without a decennial count."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if weight not in BLOCK_WEIGHTS + (WEIGHT_LAND,):
        raise ValueError(f"weight must be one of {BLOCK_WEIGHTS + (WEIGHT_LAND,)}")
    sv = "2010" if direction == "2010_to_2020" else "2020"
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
        "source_geoid": np.asarray(source_area, dtype=object),
        "target_geoid": np.asarray(target_area, dtype=object),
        "mass": mass, "land": land, "total": total,
    })
    return pieces, missing_blocks


def _normalize_pieces(pieces: pd.DataFrame, weight: str) -> "tuple[pd.DataFrame, Dict[str, int]]":
    """Aggregate pieces to pairs and normalize within each source.

    A blank ``target_geoid`` (block in no target area) counts toward the
    source's total but is dropped from the output. Blank sources are dropped.
    """
    pieces = pieces[pieces["source_geoid"].astype(str) != ""]
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
    keep = (pairs["weight"] > 0) & (pairs["target_geoid"].astype(str) != "")
    out = pairs.loc[keep, ["source_geoid", "target_geoid", "weight", "part_area", "mass"]].reset_index(drop=True)
    src = pairs["source_geoid"]
    fallbacks = {
        "no_count_land_area": int(src[use_land].nunique()) if weight != WEIGHT_LAND else 0,
        "total_area": int(src[use_total].nunique()),
        "equal_split": int(src[~use_mass & ~use_land & ~use_total].nunique()),
    }
    return out, fallbacks


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
    sv, tv = ("2010", "2020") if direction == "2010_to_2020" else ("2020", "2010")
    rel = relationships
    pieces, missing_blocks = _block_pieces(rel, counts, direction, weight,
                                           rel[f"tract_{sv}"].to_numpy(), rel[f"tract_{tv}"].to_numpy())
    out, fallbacks = _normalize_pieces(pieces, weight)
    fallbacks["blocks_without_counts"] = missing_blocks
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
# ZCTA and place crosswalks (block assignment)
# ---------------------------------------------------------------------------

ASSIGN_COLUMNS = ["block_geoid", "area_geoid"]


def _assign_frame(blocks: Iterable[str], areas: Iterable[str]) -> pd.DataFrame:
    df = pd.DataFrame({"block_geoid": pd.Series(list(blocks), dtype=str).str.strip(),
                       "area_geoid": pd.Series(list(areas), dtype=str).str.strip()})
    df = df[(df["area_geoid"] != "") & (df["area_geoid"].str.lower() != "nan") & (df["block_geoid"] != "")]
    return df.drop_duplicates("block_geoid").reset_index(drop=True)


def parse_place_assignment(text: str) -> pd.DataFrame:
    """``BlockAssign_*_INCPLACE_CDP.txt`` (2010 comma- or 2020 pipe-delimited)
    -> ``block_geoid, area_geoid`` with the 7-digit place GEOID (state + PLACEFP).
    Blocks in no place are dropped."""
    text = text.lstrip("\ufeff")
    sep = "|" if "|" in text.split("\n", 1)[0] else ","
    df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [c.strip().upper() for c in df.columns]
    if not {"BLOCKID", "PLACEFP"} <= set(df.columns):
        raise BlockDataError("place block assignment file lacks BLOCKID / PLACEFP")
    placefp = df["PLACEFP"].str.strip()
    area = np.where(placefp != "", df["BLOCKID"].str[:2] + placefp.str.zfill(5), "")
    return _assign_frame(df["BLOCKID"], area)


def parse_zcta520_blocks(lines: Iterable[str]) -> pd.DataFrame:
    """Rows of ``tab20_zcta520_tabblock20_natl.txt`` -> ``block_geoid, area_geoid``."""
    it = iter(lines)
    header = next(it).lstrip("\ufeff").rstrip("\r\n").split("|")
    try:
        zi, bi = header.index("GEOID_ZCTA5_20"), header.index("GEOID_TABBLOCK_20")
    except ValueError as exc:
        raise BlockDataError("ZCTA-to-block file lacks GEOID_ZCTA5_20 / GEOID_TABBLOCK_20") from exc
    blocks, areas = [], []
    for line in it:
        parts = line.rstrip("\r\n").split("|")
        if len(parts) > max(zi, bi) and parts[zi]:
            blocks.append(parts[bi])
            areas.append(parts[zi])
    return _assign_frame(blocks, areas)


def faces_to_zcta(faces: pd.DataFrame) -> pd.DataFrame:
    """TIGER/Line 2010 faces attributes -> 2010 ``block_geoid, area_geoid`` (ZCTA5CE10).

    Faces with no block or ZCTA are skipped; a block on faces with different
    ZCTAs (not expected; ZCTAs are built from blocks) takes the most common one
    and is counted in ``attrs["conflicts"]``."""
    need = ["STATEFP10", "COUNTYFP10", "TRACTCE10", "BLOCKCE10", "ZCTA5CE10"]
    missing = [c for c in need if c not in faces.columns]
    if missing:
        raise BlockDataError(f"faces file lacks column(s) {missing}")
    f = faces[need].fillna("").astype(str)
    f = f[(f["BLOCKCE10"].str.strip() != "") & (f["ZCTA5CE10"].str.strip() != "")]
    block = f["STATEFP10"] + f["COUNTYFP10"] + f["TRACTCE10"] + f["BLOCKCE10"].str[:4]
    pairs = pd.DataFrame({"block_geoid": block.values, "area_geoid": f["ZCTA5CE10"].values})
    counts = pairs.value_counts().reset_index(name="n").sort_values(["block_geoid", "n"], ascending=[True, False])
    conflicts = int(counts["block_geoid"].duplicated().sum())
    out = _assign_frame(counts["block_geoid"], counts["area_geoid"])
    out.attrs["conflicts"] = conflicts
    return out


def _assignment_cache(name: str) -> str:
    return os.path.join(block_cache_dir(), name)


def _read_assignment_cache(path: str) -> Optional[pd.DataFrame]:
    raw = _read_cached(path)
    if raw is None:
        return None
    return pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)


def _write_assignment_cache(path: str, df: pd.DataFrame, url: str) -> None:
    _write_cached(path, df[ASSIGN_COLUMNS].to_csv(index=False, lineterminator="\n").encode("utf-8"), url, "text/csv")


def _fetch_zcta2020(session: Any = None) -> None:
    """Stream the national 2020 ZCTA-to-block file once; cache one compact file per state."""
    if _offline():
        raise BlockDataError("2020 ZCTA block file not cached and CENSUS_BLOCK_OFFLINE is set")
    resp = polite_get(ZCTA_2020_URL, user_agent=USER_AGENT, error=BlockDataError, session=session, timeout=600,
                      stream=True)
    if resp.status_code != 200:
        raise BlockDataError(f"HTTP {resp.status_code} for {ZCTA_2020_URL}")
    lines = (ln.decode("utf-8-sig") if isinstance(ln, bytes) else ln for ln in resp.iter_lines())
    df = parse_zcta520_blocks(lines)
    if df.empty:
        raise BlockDataError(f"{ZCTA_2020_URL}: no block rows")
    for st, part in df.groupby(df["block_geoid"].str[:2]):
        if st in STATES:
            _write_assignment_cache(_assignment_cache(f"zcta520_blocks_{st}.csv"), part, ZCTA_2020_URL)


def _fetch_zcta2010(st: str, counties: Iterable[str], session: Any = None) -> pd.DataFrame:
    import tempfile

    import pyogrio

    frames = []
    for co in sorted(set(counties)):
        url = FACES_2010_URL.format(state=st, county=co)
        raw = _get(url, session=session, timeout=300)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, os.path.basename(url))
            with open(path, "wb") as f:
                f.write(raw)
            try:
                with zipfile.ZipFile(path) as zf:
                    shp = next(n for n in zf.namelist() if n.lower().endswith(".dbf"))
            except (zipfile.BadZipFile, StopIteration) as exc:
                raise BlockDataError(f"{url}: not a TIGER faces zip") from exc
            faces = pyogrio.read_dataframe(f"zip://{path}!{shp}", read_geometry=False,
                                           columns=["STATEFP10", "COUNTYFP10", "TRACTCE10", "BLOCKCE10", "ZCTA5CE10"])
        frames.append(faces_to_zcta(faces))
    return _assign_frame(pd.concat(frames)["block_geoid"], pd.concat(frames)["area_geoid"]) if frames else \
        pd.DataFrame(columns=ASSIGN_COLUMNS)


def load_block_assignment(
    state: Any,
    geography: str,
    year: int,
    counties: Optional[Iterable[str]] = None,
    session: Any = None,
) -> pd.DataFrame:
    """Block -> ZCTA or place for one state and census year (2010 or 2020), cached.

    ``counties`` (3-digit codes) is needed only for 2010 ZCTAs (one TIGER faces
    file per county); by default they come from the block relationship file.
    """
    if geography not in AREA_GEOGRAPHIES:
        raise ValueError(f"geography must be one of {AREA_GEOGRAPHIES}")
    if year not in (2010, 2020):
        raise ValueError("year must be 2010 or 2020")
    st = _state(state)
    if geography == "place":
        url = BAF_URLS[year].format(state=st, abbr=STATE_ABBR[st])
        path = _assignment_cache(f"place{year}_blocks_{st}.csv")
        cached = _read_assignment_cache(path)
        if cached is not None:
            return cached
        if _offline():
            raise BlockDataError(f"{os.path.basename(path)} not cached and CENSUS_BLOCK_OFFLINE is set")
        raw = _get(url, session=session)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                name = next(n for n in zf.namelist() if "INCPLACE_CDP" in n.upper())
                text = zf.read(name).decode("utf-8-sig")
        except (zipfile.BadZipFile, StopIteration) as exc:
            raise BlockDataError(f"{url}: no INCPLACE_CDP file in the block assignment zip") from exc
        df = parse_place_assignment(text)
        _write_assignment_cache(path, df, url)
        return df
    path = _assignment_cache(f"zcta{510 if year == 2010 else 520}_blocks_{st}.csv")
    cached = _read_assignment_cache(path)
    if cached is not None:
        return cached
    if year == 2020:
        _fetch_zcta2020(session=session)
        cached = _read_assignment_cache(path)
        return cached if cached is not None else pd.DataFrame(columns=ASSIGN_COLUMNS)
    if _offline():
        raise BlockDataError(f"{os.path.basename(path)} not cached and CENSUS_BLOCK_OFFLINE is set")
    cos = counties if counties is not None else load_block_relationships(st, session=session)["tract_2010"].str[2:5].unique()
    df = _fetch_zcta2010(st, cos, session=session)
    _write_assignment_cache(path, df, FACES_2010_URL.format(state=st, county="<each>"))
    return df


def block_area_weights(
    relationships: pd.DataFrame,
    counts: Optional[pd.DataFrame],
    assign_2010: pd.DataFrame,
    assign_2020: pd.DataFrame,
    direction: str = "2010_to_2020",
    weight: str = WEIGHT_POPULATION,
    geography: str = "area",
) -> pd.DataFrame:
    """ZCTA / place (or any block-built area) crosswalk from block pieces.

    ``assign_2010`` / ``assign_2020``: ``block_geoid, area_geoid`` for each
    vintage (:func:`load_block_assignment`). Output columns ``source_geoid,
    target_geoid, weight, part_area, mass, target_coverage``; see the module
    docstring for why weights can sum to less than 1.
    """
    return _area_weights_from_pieces([_area_pieces(relationships, counts, assign_2010, assign_2020,
                                                   direction, weight)], direction, weight, geography)


def _area_pieces(rel, counts, assign_2010, assign_2020, direction, weight):
    sv, tv = ("2010", "2020") if direction == "2010_to_2020" else ("2020", "2010")
    maps = {"2010": assign_2010, "2020": assign_2020}

    def area(v: str) -> np.ndarray:
        a = maps[v]
        lookup = pd.Series(a["area_geoid"].astype(str).to_numpy(), index=a["block_geoid"].astype(str).to_numpy())
        lookup = lookup[~lookup.index.duplicated()]
        return rel[f"block_{v}"].map(lookup).fillna("").to_numpy()

    pieces, missing = _block_pieces(rel, counts, direction, weight, area(sv), area(tv))
    pieces.attrs["blocks_without_counts"] = missing
    return pieces


def _area_weights_from_pieces(parts: List[pd.DataFrame], direction: str, weight: str, geography: str) -> pd.DataFrame:
    missing = sum(int(p.attrs.get("blocks_without_counts", 0)) for p in parts)
    pieces = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["source_geoid", "target_geoid", "mass", "land", "total"])
    out, fallbacks = _normalize_pieces(pieces, weight)
    fallbacks["blocks_without_counts"] = missing
    # Share of each target's mass (or land) that came from some source area.
    basis = "mass" if weight != WEIGHT_LAND and pieces["mass"].sum() > 0 else "land"
    tgt = pieces[pieces["target_geoid"].astype(str) != ""]
    from_src = tgt[tgt["source_geoid"].astype(str) != ""].groupby("target_geoid")[basis].sum()
    all_in = tgt.groupby("target_geoid")[basis].sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        coverage = (from_src.reindex(all_in.index).fillna(0) / all_in).where(all_in > 0, 1.0)
    out["target_coverage"] = out["target_geoid"].map(coverage).fillna(1.0).to_numpy()
    src_mass = pieces[pieces["source_geoid"].astype(str) != ""]
    total_mass = float(src_mass[basis].sum())
    unassigned = float(src_mass.loc[src_mass["target_geoid"].astype(str) == "", basis].sum())
    low = int((coverage < 0.99).sum())
    notes = [f"{weight} weights from 2010/2020 Census block counts, block relationship files and "
             f"{geography} block assignments" if weight != WEIGHT_LAND else
             f"land_area weights from block relationship pieces and {geography} block assignments"]
    if unassigned > 0:
        notes.append(f"{unassigned / total_mass:.2%} of source {basis} lies in blocks outside every target "
                     f"{geography}; those sources' weights sum to less than 1")
    if low:
        notes.append(f"{low} target {geography}(s) get under 99% of their block {basis} from source areas "
                     "(see target_coverage); apportioned totals undercount them")
    if fallbacks["no_count_land_area"]:
        notes.append(f"{fallbacks['no_count_land_area']} source {geography}(s) with no counts use land-area weights")
    out.attrs.update(weight_type=weight, direction=direction, geography=geography, fallbacks=fallbacks,
                     notes=notes, unassigned_share=(unassigned / total_mass) if total_mass else 0.0)
    return out


def area_weights(
    geography: str,
    states: Optional[Sequence[Any]] = None,
    direction: str = "2010_to_2020",
    weight: str = WEIGHT_POPULATION,
    api_key: Optional[str] = None,
    session: Any = None,
    on_error: str = "raise",
) -> pd.DataFrame:
    """ZCTA (``geography="zcta"``) or place 2010 <-> 2020 crosswalk for ``states``.

    Pieces from all states are pooled before normalizing (ZCTAs cross state
    lines). ``on_error="skip"`` leaves out failing states (``attrs["failed_states"]``);
    a ZCTA that straddles a skipped state then gets weights from the other
    state's pieces only, which the notes say.
    """
    if geography not in AREA_GEOGRAPHIES:
        raise ValueError(f"geography must be one of {AREA_GEOGRAPHIES}")
    sv = 2010 if direction == "2010_to_2020" else 2020
    parts: List[pd.DataFrame] = []
    failed: Dict[str, str] = {}
    for st in [_state(s) for s in (states or STATES)]:
        try:
            rel = load_block_relationships(st, session=session)
            rel = rel[rel[f"block_{sv}"].astype(str).str[:2] == st]
            rel = rel.drop_duplicates(["block_2010", "block_2020"])
            counties = sorted(rel["tract_2010"].astype(str).str[2:5].unique())
            counts = None if weight == WEIGHT_LAND else load_block_counts(
                st, sv, counties=sorted(rel[f"tract_{sv}"].astype(str).str[2:5].unique()), api_key=api_key,
                session=session)
            # Target blocks may lie in a neighbouring state: load its assignment too.
            a = {}
            for year in (2010, 2020):
                block_states = sorted(set(rel[f"block_{year}"].astype(str).str[:2]) & set(STATES))
                a[year] = pd.concat([load_block_assignment(
                    b, geography, year, counties=counties if b == st else None, session=session)
                    for b in block_states], ignore_index=True)
            parts.append(_area_pieces(rel, counts, a[2010], a[2020], direction, weight))
        except (BlockDataError, OSError, ValueError) as exc:
            if on_error == "raise":
                raise
            failed[st] = str(exc)
    out = _area_weights_from_pieces(parts, direction, weight, geography)
    if failed:
        out.attrs["notes"].append(f"block data unavailable for state(s) {', '.join(sorted(failed))}; "
                                  f"{geography}s there are missing or use only other states' pieces")
    out.attrs.update(
        failed_states=failed,
        source={"relationship_files": REL_URL,
                "assignment": ({2010: FACES_2010_URL, 2020: ZCTA_2020_URL} if geography == "zcta" else BAF_URLS),
                "counts": {y: f"{BASE_URL}/{y}/{d} {p},{h}" for y, (d, p, h) in DECENNIAL_COUNTS.items()}},
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
