# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census boundary files for mapping and areal work (PLAN_ROUND_9.md, workstream A).

* ``get_boundaries(level, year, state=None, detail="cb")`` downloads (or reads
  from ``.cache/geo/<year>/``) a Census cartographic boundary file
  (``detail="cb"``, 1:500,000, generalized for maps) or a TIGER/Line file
  (``detail="tiger"``, full resolution, for areal calculations and
  crosswalks). Each cached zip has a ``.json`` sidecar with source URL and
  SHA-256; a file whose hash does not match is downloaded again.
* ``boundary_vintage(acs_year, level)`` gives the boundary year that matches
  an ACS 5-year release; ``check_boundary_vintage`` raises
  ``BoundaryVintageError`` on a mismatch (never a silent join).
* ``join_boundaries(frame, level, year, ...)`` joins ACS rows to polygons on
  GEOID and reports unmatched GEOIDs on both sides.
* ``write_geo(gdf, path, format="gpkg")``: GeoPackage (default), GeoParquet,
  or shapefile on request (with a warning about 10-character field names).
* ``equal_area_crs(gdf)``: EPSG:5070 (CONUS Albers), EPSG:3338 (Alaska
  Albers), ESRI:102007 (Hawaii Albers) or a Puerto Rico Albers definition.

Boundaries are returned in the source CRS (NAD83, EPSG:4269), sorted by GEOID.

URL patterns (``BOUNDARY_SOURCES``) were checked with HTTP HEAD requests
against www2.census.gov on 2026-11-02 (``VERIFIED_URLS``):

* cartographic boundary, 2014 and later:
  ``https://www2.census.gov/geo/tiger/GENZ<Y>/shp/cb_<Y>_<st|us>_<name>_500k.zip``
* cartographic boundary, 2013: no ``shp/`` directory
  (``GENZ2013/cb_2013_<st|us>_<name>_500k.zip``)
* TIGER/Line: ``https://www2.census.gov/geo/tiger/TIGER<Y>/<DIR>/tl_<Y>_<st|us>_<name>.zip``

Vintage rule: an ACS 5-year release ending in year Y is tabulated for the
geography in effect on January 1 of Y, which is the geography of the year-Y
boundary files (Census, ACS Geography Handbook / "Geography and the ACS").
Hence 2010-2019 releases carry 2010 Census tracts and 2020+ releases carry
2020 tracts (``census.geo_crosswalk.tract_vintage``), and Connecticut's
planning-region counties appear from the 2022 release. Cartographic ZCTA
files at 1:500k were published for 2013-2019 (2010 ZCTAs, ``zcta510``) and
only for 2020 in the 2020s (``zcta520``, unchanged through the decade), so
ACS 2020+ ZCTA maps use the 2020 cb file.
"""

import hashlib
import json
import os
import re
import time
import warnings
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd

from .geo import get_resource_path
from .geo_crosswalk import tract_vintage

USER_AGENT = "acs-batch-downloader/boundaries (research toolkit; python-requests)"
BASE_URL = "https://www2.census.gov/geo/tiger"
SOURCE_CRS = "EPSG:4269"
DETAILS = ("cb", "tiger")
FIRST_CB_YEAR = 2013
FIRST_TIGER_YEAR = 2011
FIRST_NATIONAL_SUBSTATE_CB_YEAR = 2019  # cb_<Y>_us_tract/bg/place/cousub
CB_ZCTA_DECADE_YEAR = 2020  # only cb ZCTA file of the 2020s

# Canonical level -> (cb name, TIGER directory, TIGER name, per-state?, GEOID length)
LEVELS: Dict[str, Dict[str, Any]] = {
    "state": {"cb": "state", "tiger_dir": "STATE", "tiger": "state", "per_state": False, "geoid_len": 2},
    "county": {"cb": "county", "tiger_dir": "COUNTY", "tiger": "county", "per_state": False, "geoid_len": 5},
    "county subdivision": {"cb": "cousub", "tiger_dir": "COUSUB", "tiger": "cousub", "per_state": True,
                           "geoid_len": 10},
    "tract": {"cb": "tract", "tiger_dir": "TRACT", "tiger": "tract", "per_state": True, "geoid_len": 11},
    "block group": {"cb": "bg", "tiger_dir": "BG", "tiger": "bg", "per_state": True, "geoid_len": 12},
    "place": {"cb": "place", "tiger_dir": "PLACE", "tiger": "place", "per_state": True, "geoid_len": 7},
    "zcta": {"cb": "zcta", "tiger_dir": "ZCTA", "tiger": "zcta", "per_state": False, "geoid_len": 5},
    "cbsa": {"cb": "cbsa", "tiger_dir": "CBSA", "tiger": "cbsa", "per_state": False, "geoid_len": 5},
}
_ALIASES = {
    "bg": "block group", "block_group": "block group", "blockgroup": "block group",
    "cousub": "county subdivision", "county_subdivision": "county subdivision",
    "zip code tabulation area": "zcta", "zcta5": "zcta",
    "msa": "cbsa", "metropolitan statistical area/micropolitan statistical area": "cbsa",
    "states": "state", "counties": "county", "tracts": "tract",
}

BOUNDARY_SOURCES: Dict[str, Dict[str, Any]] = {
    "cb": {
        "description": "Census cartographic boundary files, 1:500,000 (generalized for mapping)",
        "url_2014_plus": BASE_URL + "/GENZ{year}/shp/cb_{year}_{area}_{name}_500k.zip",
        "url_2013": BASE_URL + "/GENZ2013/cb_2013_{area}_{name}_500k.zip",
        "years": f"{FIRST_CB_YEAR}+",
        "national_substate_from": FIRST_NATIONAL_SUBSTATE_CB_YEAR,
        "zcta": "zcta510 for 2013-2019; zcta520 published for 2020 only",
        "documentation": "https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html",
    },
    "tiger": {
        "description": "TIGER/Line shapefiles (full resolution, includes water areas)",
        "url": BASE_URL + "/TIGER{year}/{dir}/tl_{year}_{area}_{name}.zip",
        "years": f"{FIRST_TIGER_YEAR}+",
        "zcta": "ZCTA5/tl_<Y>_us_zcta510 through 2019; ZCTA520/tl_<Y>_us_zcta520 from 2020",
        "documentation": "https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html",
    },
    "vintage_rule": ("ACS 5-year release Y uses the geography in effect on January 1 of Y "
                     "(boundary year Y); see https://www.census.gov/programs-surveys/acs/geography-acs.html"),
}

# HTTP 200 on HEAD, 2026-11-02 (404 noted where a plausible name does not exist).
VERIFIED_URLS: List[Tuple[str, int]] = [
    ("GENZ2013/cb_2013_us_county_500k.zip", 200),
    ("GENZ2013/shp/cb_2013_us_county_500k.zip", 404),
    ("GENZ2013/cb_2013_11_tract_500k.zip", 200),
    ("GENZ2013/cb_2013_us_zcta510_500k.zip", 200),
    ("GENZ2014/shp/cb_2014_us_county_500k.zip", 200),
    ("GENZ2014/shp/cb_2014_11_bg_500k.zip", 200),
    ("GENZ2018/shp/cb_2018_us_tract_500k.zip", 404),
    ("GENZ2019/shp/cb_2019_06_tract_500k.zip", 200),
    ("GENZ2019/shp/cb_2019_us_tract_500k.zip", 200),
    ("GENZ2019/shp/cb_2019_us_zcta510_500k.zip", 200),
    ("GENZ2019/shp/cb_2019_us_cousub_500k.zip", 200),
    ("GENZ2020/shp/cb_2020_us_zcta520_500k.zip", 200),
    ("GENZ2021/shp/cb_2021_us_zcta520_500k.zip", 404),
    ("GENZ2022/shp/cb_2022_us_tract_500k.zip", 200),
    ("GENZ2023/shp/cb_2023_11_tract_500k.zip", 200),
    ("GENZ2023/shp/cb_2023_11_bg_500k.zip", 200),
    ("GENZ2023/shp/cb_2023_11_place_500k.zip", 200),
    ("GENZ2023/shp/cb_2023_us_cbsa_500k.zip", 200),
    ("GENZ2024/shp/cb_2024_us_county_500k.zip", 200),
    ("GENZ2025/shp/cb_2025_us_county_500k.zip", 200),
    ("TIGER2011/TRACT/tl_2011_11_tract.zip", 200),
    ("TIGER2012/ZCTA5/tl_2012_us_zcta510.zip", 200),
    ("TIGER2019/ZCTA5/tl_2019_us_zcta510.zip", 200),
    ("TIGER2020/ZCTA520/tl_2020_us_zcta520.zip", 200),
    ("TIGER2023/STATE/tl_2023_us_state.zip", 200),
    ("TIGER2023/COUNTY/tl_2023_us_county.zip", 200),
    ("TIGER2023/TRACT/tl_2023_11_tract.zip", 200),
    ("TIGER2023/BG/tl_2023_11_bg.zip", 200),
    ("TIGER2023/PLACE/tl_2023_11_place.zip", 200),
    ("TIGER2023/COUSUB/tl_2023_11_cousub.zip", 200),
    ("TIGER2023/CBSA/tl_2023_us_cbsa.zip", 200),
]

# Equal-area projections by region.
CRS_CONUS = "EPSG:5070"  # NAD83 / Conus Albers
CRS_ALASKA = "EPSG:3338"  # NAD83 / Alaska Albers
CRS_HAWAII = "ESRI:102007"  # Hawaii Albers Equal Area Conic
CRS_PUERTO_RICO = ("+proj=aea +lat_0=17.5 +lon_0=-66.5 +lat_1=18.0 +lat_2=18.5 "
                   "+x_0=0 +y_0=0 +datum=NAD83 +units=m +no_defs")
REGION_STATES = {"alaska": {"02"}, "hawaii": {"15"}, "puerto_rico": {"72"}}
ISLAND_AREAS = {"60", "66", "69", "74", "78"}


class BoundaryError(RuntimeError):
    """A boundary file could not be resolved, downloaded or validated."""


class BoundaryVintageError(ValueError):
    """Boundary geography does not match the ACS release's geography."""


# --------------------------------------------------------------------------- levels and URLs

def normalize_level(level: str) -> str:
    key = str(level).strip().lower()
    key = _ALIASES.get(key, key)
    if key not in LEVELS:
        raise ValueError(f"unsupported boundary level {level!r}; use one of {sorted(LEVELS)}")
    return key


def _zcta_name(year: int, detail: str) -> str:
    return "zcta510" if year < 2020 else "zcta520"


def boundary_vintage(acs_year: int, level: str, detail: str = "cb") -> int:
    """Boundary file year that matches an ACS 5-year release.

    The ACS 5-year release ending in ``acs_year`` is tabulated for the
    geography of January 1 of that year, so the answer is ``acs_year``,
    except for cartographic ZCTAs in the 2020s, where 2020 is the only cb
    vintage published (ZCTAs are fixed for the decade). Raises ``ValueError``
    for releases older than the files this module supports.
    """
    lvl = normalize_level(level)
    year = int(acs_year)
    detail = _detail(detail)
    tract_vintage(year)  # validates: 2009 and earlier use 2000 tracts
    first = FIRST_CB_YEAR if detail == "cb" else FIRST_TIGER_YEAR
    if lvl == "zcta" and detail == "tiger":
        first = 2012
    if year < first:
        hint = " (use detail='tiger' for 2011-2012)" if detail == "cb" and year >= FIRST_TIGER_YEAR else ""
        raise ValueError(f"no {detail} {lvl} boundary file for ACS {year}; earliest is {first}{hint}")
    if lvl == "zcta" and detail == "cb" and year >= CB_ZCTA_DECADE_YEAR:
        return CB_ZCTA_DECADE_YEAR
    return year


def _ct_regime(year: int) -> str:
    return "planning regions" if year >= 2022 else "historical counties"


def check_boundary_vintage(acs_year: int, boundary_year: int, level: str, detail: str = "cb") -> int:
    """Raise ``BoundaryVintageError`` unless ``boundary_year`` is the vintage
    ``boundary_vintage(acs_year, level, detail)``; return it otherwise."""
    expected = boundary_vintage(acs_year, level, detail)
    if int(boundary_year) == expected:
        return expected
    lvl = normalize_level(level)
    why = []
    if lvl in ("tract", "block group"):
        tv_a = tract_vintage(int(acs_year))
        try:
            tv_b = tract_vintage(int(boundary_year))
        except ValueError:
            tv_b = None
        if tv_a != tv_b:
            why.append(f"ACS {acs_year} uses {tv_a} Census tracts, boundaries {boundary_year} use {tv_b} tracts")
    if _ct_regime(int(acs_year)) != _ct_regime(int(boundary_year)) and lvl != "state":
        why.append(f"Connecticut uses {_ct_regime(int(acs_year))} in ACS {acs_year} but "
                   f"{_ct_regime(int(boundary_year))} in {boundary_year} boundaries")
    raise BoundaryVintageError(
        f"{lvl} boundaries from {boundary_year} do not match ACS 5-year {acs_year}; use "
        f"{expected} boundaries" + (" (" + "; ".join(why) + ")" if why else
                                    " (geography is tabulated as of January 1 of the release year)"))


def _detail(detail: str) -> str:
    d = str(detail).lower()
    if d not in DETAILS:
        raise ValueError(f"detail must be one of {DETAILS}")
    return d


def _state(state: Optional[Union[str, int]]) -> Optional[str]:
    if state in (None, "", "us"):
        return None
    s = str(state).strip()
    if not s.isdigit():
        from .geo import FIPS_MAPPING

        s = {k.lower(): v for k, v in FIPS_MAPPING.items()}.get(s.lower(), s)
    if not str(s).isdigit():
        raise ValueError(f"state must be a 2-digit FIPS code, got {state!r}")
    return str(s).zfill(2)


def boundary_url(level: str, year: int, state: Optional[Union[str, int]] = None,
                 detail: str = "cb") -> str:
    """Download URL for one boundary file (see ``BOUNDARY_SOURCES``)."""
    lvl = normalize_level(level)
    detail = _detail(detail)
    year = int(year)
    spec = LEVELS[lvl]
    st = _state(state)
    if detail == "cb":
        if year < FIRST_CB_YEAR:
            raise ValueError(f"cartographic boundary files start in {FIRST_CB_YEAR}")
        name = _zcta_name(year, detail) if lvl == "zcta" else spec["cb"]
        if lvl == "zcta" and year > CB_ZCTA_DECADE_YEAR:
            raise ValueError(f"no cb ZCTA file for {year}; use {CB_ZCTA_DECADE_YEAR} "
                             "(boundary_vintage) or detail='tiger'")
        if spec["per_state"]:
            if st is None and year < FIRST_NATIONAL_SUBSTATE_CB_YEAR:
                raise ValueError(f"national cb {lvl} files start in {FIRST_NATIONAL_SUBSTATE_CB_YEAR}; "
                                 "pass state")
            area = st or "us"
        else:
            area = "us"
        tmpl = BOUNDARY_SOURCES["cb"]["url_2013" if year == 2013 else "url_2014_plus"]
        return tmpl.format(year=year, area=area, name=name)
    if year < FIRST_TIGER_YEAR:
        raise ValueError(f"TIGER/Line files are supported from {FIRST_TIGER_YEAR}")
    if spec["per_state"]:
        if st is None:
            raise ValueError(f"TIGER/Line {lvl} files are per state; pass state")
        area = st
    else:
        area = "us"
    if lvl == "zcta":
        name = _zcta_name(year, detail)
        tdir = "ZCTA5" if year < 2020 else "ZCTA520"
    else:
        name, tdir = spec["tiger"], spec["tiger_dir"]
    return BOUNDARY_SOURCES["tiger"]["url"].format(year=year, dir=tdir, area=area, name=name)


# --------------------------------------------------------------------------- cache

def geo_cache_dir(year: Optional[int] = None) -> str:
    """$CENSUS_GEO_DIR or <repo>/.cache/geo, optionally with a year subdirectory."""
    base = os.getenv("CENSUS_GEO_DIR") or get_resource_path(os.path.join(".cache", "geo"))
    return os.path.join(base, str(year)) if year is not None else base


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sidecar(path: str) -> str:
    return path + ".json"


def _read_cached(path: str) -> Optional[Dict[str, Any]]:
    """Sidecar metadata if ``path`` exists and matches its SHA-256."""
    side = _sidecar(path)
    if not (os.path.exists(path) and os.path.exists(side)):
        return None
    try:
        with open(side, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    with open(path, "rb") as f:
        return meta if meta.get("sha256") == _sha256(f.read()) else None


def write_cache_entry(path: str, raw: bytes, url: str, content_type: str = "application/zip") -> Dict[str, Any]:
    """Write ``raw`` to ``path`` atomically with a SHA-256 sidecar."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)
    meta = {"source_url": url, "sha256": _sha256(raw), "size_bytes": len(raw),
            "content_type": content_type,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(_sidecar(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


def _download(url: str, session: Any = None, timeout: float = 300.0) -> Tuple[bytes, str]:
    import requests

    getter = session if session is not None else requests
    try:
        resp = getter.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:  # pragma: no cover - network failure
        raise BoundaryError(f"Download failed for {url}: {exc}") from exc
    raw = resp.content or b""
    if b"Request Rejected" in raw[:2000]:
        raise BoundaryError(f"census.gov rejected the request for {url}; wait and retry later")
    if resp.status_code == 404:
        raise BoundaryError(f"Not published (HTTP 404): {url}")
    if resp.status_code != 200:
        raise BoundaryError(f"HTTP {resp.status_code} for {url}")
    if raw[:2] != b"PK":
        raise BoundaryError(f"{url} did not return a zip archive")
    return raw, str(resp.headers.get("Content-Type", ""))


# --------------------------------------------------------------------------- boundaries

def _normalize_geoid_column(gdf):
    if "GEOID" not in gdf.columns:
        cand = [c for c in gdf.columns if re.fullmatch(r"GEOID\d{2}", str(c))]
        if not cand:
            raise BoundaryError(f"boundary file has no GEOID column: {list(gdf.columns)}")
        gdf = gdf.rename(columns={cand[0]: "GEOID"})
    gdf["GEOID"] = gdf["GEOID"].astype(str)
    return gdf


def get_boundaries(level: str, year: int, state: Optional[Union[str, int]] = None,
                   detail: str = "cb", *, cache_dir: Optional[str] = None, refresh: bool = False,
                   session: Any = None):
    """Boundaries for ``level`` in boundary year ``year`` as a GeoDataFrame.

    ``state`` (FIPS code or state name) selects a per-state file for tract, block
    group, place and county subdivision (required for TIGER/Line, and for cb
    before 2019); for national levels it filters rows. ``year`` is the
    boundary year: use ``boundary_vintage(acs_year, level)`` to pick it.

    Cached under ``$CENSUS_GEO_DIR`` (default ``.cache/geo``) as
    ``<year>/<file>.zip`` plus ``<file>.zip.json`` (source URL, SHA-256);
    a cached file whose hash does not match is downloaded again. The frame is
    in NAD83 (EPSG:4269), sorted by GEOID, and ``attrs`` carry ``level``,
    ``year``, ``detail``, ``source_url``, ``sha256`` and ``cache_path``.
    """
    import geopandas as gpd

    lvl = normalize_level(level)
    detail = _detail(detail)
    year = int(year)
    st = _state(state)
    file_state = st if LEVELS[lvl]["per_state"] else None
    url = boundary_url(lvl, year, file_state, detail)
    path = os.path.join(cache_dir or geo_cache_dir(), str(year), url.rsplit("/", 1)[-1])
    meta = None if refresh else _read_cached(path)
    if meta is None:
        raw, ctype = _download(url, session=session)
        meta = write_cache_entry(path, raw, url, ctype or "application/zip")
    gdf = gpd.read_file(path)
    gdf = _normalize_geoid_column(gdf)
    if st is not None and not LEVELS[lvl]["per_state"] and lvl not in ("zcta", "cbsa"):
        gdf = gdf[gdf["GEOID"].str.startswith(st)]
    gdf = gdf.sort_values("GEOID", kind="mergesort").reset_index(drop=True)
    gdf.attrs.update(level=lvl, year=year, detail=detail, state=st, source_url=url,
                     sha256=meta.get("sha256"), cache_path=path,
                     description=boundary_description(lvl, year, detail))
    return gdf


def boundary_description(level: str, year: int, detail: str = "cb") -> str:
    """Caption text such as 'Census 2023 cartographic boundary file (1:500,000), tract'."""
    kind = "cartographic boundary file (1:500,000)" if detail == "cb" else "TIGER/Line shapefile"
    return f"Census {year} {kind}, {normalize_level(level)}"


# --------------------------------------------------------------------------- joins

def normalize_geoids(values: Iterable[Any], level: str) -> pd.Series:
    """GEOIDs as zero-padded strings; ``1400000US``-style prefixes removed."""
    n = LEVELS[normalize_level(level)]["geoid_len"]
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and v != v):
            out.append(None)
            continue
        s = str(v).strip()
        if "US" in s:
            s = s.split("US", 1)[1]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        out.append(s.zfill(n) if s.isdigit() else s)
    return pd.Series(out, dtype=object)


def join_boundaries(frame: pd.DataFrame, level: str, year: Optional[int] = None, *,
                    acs_year: Optional[int] = None, boundaries=None, geoid_col: str = "GEOID",
                    state: Optional[Union[str, int]] = None, detail: str = "cb", how: str = "left",
                    on_unmatched: str = "warn", cache_dir: Optional[str] = None, session: Any = None):
    """Join ACS rows to boundary polygons on GEOID.

    ``acs_year`` (default ``frame.attrs["year"]`` when present) is checked
    against the boundary year with ``check_boundary_vintage``: a mismatch
    raises ``BoundaryVintageError``. ``year`` defaults to
    ``boundary_vintage(acs_year, level)``. ``boundaries`` may be a
    GeoDataFrame already loaded (its ``attrs["year"]`` is checked).

    ``how="left"`` keeps every polygon (areas without data map as "no data");
    ``"inner"`` keeps matches only. Unmatched GEOIDs on either side are
    reported in ``result.attrs["join_report"]`` (``matched``,
    ``unmatched_data``, ``unmatched_boundaries``) and, per ``on_unmatched``
    (``warn``, ``raise``, ``ignore``), warned about or raised.
    """
    import geopandas as gpd

    lvl = normalize_level(level)
    if how not in ("left", "inner"):
        raise ValueError("how must be 'left' or 'inner'")
    if on_unmatched not in ("warn", "raise", "ignore"):
        raise ValueError("on_unmatched must be 'warn', 'raise' or 'ignore'")
    if acs_year is None:
        acs_year = frame.attrs.get("year") or frame.attrs.get("release_year")
    if boundaries is not None:
        b_year = boundaries.attrs.get("year", year)
        detail = boundaries.attrs.get("detail", detail)
        if year is not None and b_year is not None and int(year) != int(b_year):
            raise BoundaryVintageError(f"year={year} but the boundaries are from {b_year}")
        year = b_year
    if year is None:
        if acs_year is None:
            raise ValueError("give year (boundary year) or acs_year")
        year = boundary_vintage(int(acs_year), lvl, detail)
    if acs_year is not None:
        check_boundary_vintage(int(acs_year), int(year), lvl, detail)
    if boundaries is None:
        boundaries = get_boundaries(lvl, int(year), state, detail, cache_dir=cache_dir, session=session)
    data = frame.copy()
    data["GEOID"] = normalize_geoids(data[geoid_col], lvl).to_numpy()
    if geoid_col != "GEOID":
        data = data.drop(columns=[geoid_col])
    dup = data["GEOID"][data["GEOID"].duplicated()].unique().tolist()
    if dup:
        raise ValueError(f"frame has duplicate GEOIDs: {dup[:10]}")
    geo = boundaries.copy()
    geo["GEOID"] = normalize_geoids(geo["GEOID"], lvl).to_numpy()
    overlap = [c for c in data.columns if c in geo.columns and c != "GEOID"]
    geo = geo.drop(columns=[c for c in overlap if c != "geometry"])
    d_ids, g_ids = set(data["GEOID"].dropna()), set(geo["GEOID"].dropna())
    unmatched_data = sorted(d_ids - g_ids)
    unmatched_geo = sorted(g_ids - d_ids)
    report = {"level": lvl, "boundary_year": int(year), "acs_year": None if acs_year is None else int(acs_year),
              "detail": detail, "n_data": len(d_ids), "n_boundaries": len(g_ids),
              "matched": len(d_ids & g_ids), "unmatched_data": unmatched_data,
              "unmatched_boundaries": unmatched_geo}
    msg = (f"{len(unmatched_data)} data GEOIDs have no {year} {lvl} boundary "
           f"(e.g. {unmatched_data[:5]}); {len(unmatched_geo)} boundaries have no data row")
    if unmatched_data or unmatched_geo:
        if on_unmatched == "raise":
            raise ValueError(msg)
        if on_unmatched == "warn" and (unmatched_data or how == "left"):
            warnings.warn(msg, stacklevel=2)
    merged = geo.merge(data, on="GEOID", how=how, sort=False)
    merged = gpd.GeoDataFrame(merged, geometry="geometry", crs=boundaries.crs)
    merged = merged.sort_values("GEOID", kind="mergesort").reset_index(drop=True)
    merged.attrs.update({k: v for k, v in boundaries.attrs.items()})
    merged.attrs["acs_year"] = report["acs_year"]
    merged.attrs["join_report"] = report
    return merged


# --------------------------------------------------------------------------- output and CRS

GEO_FORMATS = {"gpkg": ("GPKG", ".gpkg"), "parquet": (None, ".parquet"),
               "shp": ("ESRI Shapefile", ".shp")}


def write_geo(gdf, path: Union[str, os.PathLike], format: Optional[str] = None,
              layer: Optional[str] = None) -> str:
    """Write a GeoDataFrame as GeoPackage (default), GeoParquet or shapefile.

    ``format`` defaults from the extension (``.gpkg``, ``.parquet``,
    ``.shp``), else ``gpkg``. Shapefiles truncate field names to 10
    characters (ACS codes such as ``B19013_001E_moe`` collide), so writing one
    warns and lists the truncated names; prefer GeoPackage, which ArcGIS Pro
    and QGIS open natively.
    """
    p = str(path)
    fmt = (format or {".gpkg": "gpkg", ".parquet": "parquet", ".geoparquet": "parquet",
                      ".shp": "shp"}.get(os.path.splitext(p)[1].lower(), "gpkg")).lower()
    if fmt == "geoparquet":
        fmt = "parquet"
    if fmt not in GEO_FORMATS:
        raise ValueError(f"format must be one of {sorted(GEO_FORMATS)}")
    driver, ext = GEO_FORMATS[fmt]
    if not p.lower().endswith(ext):
        p += ext
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    if fmt == "parquet":
        gdf.to_parquet(p, index=False)
        return p
    if fmt == "shp":
        long = [str(c) for c in gdf.columns if c != gdf.geometry.name and len(str(c)) > 10]
        if long:
            short: Dict[str, List[str]] = {}
            for c in long:
                short.setdefault(c[:10], []).append(c)
            coll = {k: v for k, v in short.items() if len(v) > 1 or k in gdf.columns}
            warnings.warn(
                "shapefile field names are limited to 10 characters; truncated: "
                + ", ".join(long[:20]) + (f"; COLLISIONS: {coll}" if coll else "")
                + ". Use GeoPackage (format='gpkg') to keep full names.", stacklevel=2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # already reported above; GDAL launders collisions
            gdf.to_file(p, driver=driver)
        return p
    gdf.to_file(p, driver=driver, layer=layer or os.path.splitext(os.path.basename(p))[0])
    return p


def _state_codes(gdf) -> Optional[pd.Series]:
    for c in ("STATEFP", "STATEFP20", "STATEFP10", "GEOID"):
        if c in gdf.columns:
            s = gdf[c].astype(str).str[:2]
            if s.str.isdigit().all():
                return s
    return None


def region_of(gdf) -> pd.Series:
    """Per-row region: ``conus``, ``alaska``, ``hawaii``, ``puerto_rico`` or
    ``other`` (island areas), from state FIPS or, failing that, longitude/latitude."""
    st = _state_codes(gdf)
    if st is None:
        pts = gdf.geometry.to_crs(SOURCE_CRS).representative_point() if gdf.crs else gdf.geometry.representative_point()
        lon, lat = pts.x.to_numpy(), pts.y.to_numpy()
        reg = []
        for x, y in zip(lon, lat):
            if y > 50 or x > 170:
                reg.append("alaska")
            elif x < -150 and y < 30:
                reg.append("hawaii")
            elif -68.5 < x < -64 and 17 < y < 19:
                reg.append("puerto_rico")
            elif y < 24 or x > -60:
                reg.append("other")
            else:
                reg.append("conus")
        return pd.Series(reg, index=gdf.index)
    out = pd.Series("conus", index=gdf.index)
    for name, codes in REGION_STATES.items():
        out[st.isin(codes)] = name
    out[st.isin(ISLAND_AREAS)] = "other"
    return out


def crs_for_region(region: str):
    import pyproj

    return pyproj.CRS.from_user_input({"conus": CRS_CONUS, "alaska": CRS_ALASKA, "hawaii": CRS_HAWAII,
                                       "puerto_rico": CRS_PUERTO_RICO}.get(region, CRS_CONUS))


def equal_area_crs(gdf):
    """Equal-area CRS for area and distance work on ``gdf``.

    All rows in Alaska -> EPSG:3338; Hawaii -> ESRI:102007; Puerto Rico ->
    an Albers definition centred on the island; otherwise (CONUS or a mix)
    EPSG:5070. Island areas alone get a Lambert azimuthal equal-area
    projection centred on the data.
    """
    import pyproj

    regions = set(region_of(gdf))
    if len(regions) == 1:
        (r,) = regions
        if r == "other":
            c = gdf.to_crs(SOURCE_CRS).geometry.union_all().centroid
            return pyproj.CRS.from_proj4(f"+proj=laea +lat_0={c.y:.4f} +lon_0={c.x:.4f} "
                                         "+datum=NAD83 +units=m +no_defs")
        return crs_for_region(r)
    return crs_for_region("conus")
