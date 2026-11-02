"""Geographic reference data and place parsing utilities for US Census ACS."""

import json
import os
import sys
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd


def get_resource_path(relative_path: str) -> str:
    """Get the path to a resource in either development or packaged application."""
    if getattr(sys, "_MEIPASS", False):
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        # Default to the workspace root directory (one level up from this file)
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


STATE_FILES: List[str] = [
    "st01_al_place2020.txt",
    "st02_ak_place2020.txt",
    "st04_az_place2020.txt",
    "st05_ar_place2020.txt",
    "st06_ca_place2020.txt",
    "st08_co_place2020.txt",
    "st09_ct_place2020.txt",
    "st10_de_place2020.txt",
    "st11_dc_place2020.txt",
    "st12_fl_place2020.txt",
    "st13_ga_place2020.txt",
    "st15_hi_place2020.txt",
    "st16_id_place2020.txt",
    "st17_il_place2020.txt",
    "st18_in_place2020.txt",
    "st19_ia_place2020.txt",
    "st20_ks_place2020.txt",
    "st21_ky_place2020.txt",
    "st22_la_place2020.txt",
    "st23_me_place2020.txt",
    "st24_md_place2020.txt",
    "st25_ma_place2020.txt",
    "st26_mi_place2020.txt",
    "st27_mn_place2020.txt",
    "st28_ms_place2020.txt",
    "st29_mo_place2020.txt",
    "st30_mt_place2020.txt",
    "st31_ne_place2020.txt",
    "st32_nv_place2020.txt",
    "st33_nh_place2020.txt",
    "st34_nj_place2020.txt",
    "st35_nm_place2020.txt",
    "st36_ny_place2020.txt",
    "st37_nc_place2020.txt",
    "st38_nd_place2020.txt",
    "st39_oh_place2020.txt",
    "st40_ok_place2020.txt",
    "st41_or_place2020.txt",
    "st42_pa_place2020.txt",
    "st44_ri_place2020.txt",
    "st45_sc_place2020.txt",
    "st46_sd_place2020.txt",
    "st47_tn_place2020.txt",
    "st48_tx_place2020.txt",
    "st49_ut_place2020.txt",
    "st50_vt_place2020.txt",
    "st51_va_place2020.txt",
    "st53_wa_place2020.txt",
    "st54_wv_place2020.txt",
    "st55_wi_place2020.txt",
    "st56_wy_place2020.txt",
    "st60_as_place2020.txt",
    "st66_gu_place2020.txt",
    "st69_mp_place2020.txt",
    "st72_pr_place2020.txt",
    "st78_vi_place2020.txt",
]

STATE_MAPPING: Dict[str, str] = {
    "01": "Alabama",
    "02": "Alaska",
    "04": "Arizona",
    "05": "Arkansas",
    "06": "California",
    "08": "Colorado",
    "09": "Connecticut",
    "10": "Delaware",
    "11": "District of Columbia",
    "12": "Florida",
    "13": "Georgia",
    "15": "Hawaii",
    "16": "Idaho",
    "17": "Illinois",
    "18": "Indiana",
    "19": "Iowa",
    "20": "Kansas",
    "21": "Kentucky",
    "22": "Louisiana",
    "23": "Maine",
    "24": "Maryland",
    "25": "Massachusetts",
    "26": "Michigan",
    "27": "Minnesota",
    "28": "Mississippi",
    "29": "Missouri",
    "30": "Montana",
    "31": "Nebraska",
    "32": "Nevada",
    "33": "New Hampshire",
    "34": "New Jersey",
    "35": "New Mexico",
    "36": "New York",
    "37": "North Carolina",
    "38": "North Dakota",
    "39": "Ohio",
    "40": "Oklahoma",
    "41": "Oregon",
    "42": "Pennsylvania",
    "44": "Rhode Island",
    "45": "South Carolina",
    "46": "South Dakota",
    "47": "Tennessee",
    "48": "Texas",
    "49": "Utah",
    "50": "Vermont",
    "51": "Virginia",
    "53": "Washington",
    "54": "West Virginia",
    "55": "Wisconsin",
    "56": "Wyoming",
    "60": "American Samoa",
    "66": "Guam",
    "69": "Northern Mariana Islands",
    "72": "Puerto Rico",
    "74": "U.S. Minor Outlying Islands",
    "78": "U.S. Virgin Islands",
}

FIPS_MAPPING: Dict[str, str] = {v: k for k, v in STATE_MAPPING.items()}


def extract_state_abbr_from_filename(filename: str) -> str:
    """Extract numeric two-digit state FIPS code from place filename."""
    return filename[2:4]


def get_local_places_file(
    state_abbr: str, base_dir: Optional[str] = None
) -> Optional[str]:
    """Return the absolute path to the local places file for a state code."""
    matching_files = [file for file in STATE_FILES if f"st{state_abbr}" in file]
    if matching_files:
        if base_dir:
            return os.path.join(base_dir, matching_files[0])
        return get_resource_path(os.path.join("state_places", matching_files[0]))
    return None


def load_places_from_local(path: str) -> List[Tuple[str, str, str, str, str]]:
    """Load place entries from a pipe-delimited Census place definition file."""
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()
    lines = lines[1:]  # Skip header

    places: List[Tuple[str, str, str, str, str]] = []
    for line in lines:
        parts = line.strip().split("|")
        if len(parts) >= 9:
            _, state_code, place_code, _, place_name, place_type, _, _, counties = (
                parts[:9]
            )
            places.append((state_code, place_code, place_name, place_type, counties))
    return places


# ---------------------------------------------------------------------------
# Composite GEOIDs (TIGER/Line join keys)
# ---------------------------------------------------------------------------

# Census API response column names for geography components
ZCTA_COLUMN = "zip code tabulation area"
MSA_COLUMN = "metropolitan statistical area/micropolitan statistical area"

# geo level -> ordered (column, width) components making up its GEOID
GEOID_COMPONENTS: Dict[str, List[Tuple[str, int]]] = {
    "state": [("state", 2)],
    "county": [("state", 2), ("county", 3)],
    "tract": [("state", 2), ("county", 3), ("tract", 6)],
    "place": [("state", 2), ("place", 5)],
    "zcta": [(ZCTA_COLUMN, 5)],
    "msa": [(MSA_COLUMN, 5)],
}

# Geography columns that may appear in API responses (never numeric data)
GEOGRAPHY_COLUMNS: List[str] = [
    "GEO_ID",
    "GEOID",
    "NAME",
    "us",
    "state",
    "county",
    "tract",
    "place",
    ZCTA_COLUMN,
    MSA_COLUMN,
]


def detect_geo_level(columns: Iterable[str]) -> Optional[str]:
    """Infer the geography level from API response columns (most specific first)."""
    cols = set(columns)
    if "tract" in cols:
        return "tract"
    if ZCTA_COLUMN in cols:
        return "zcta"
    if MSA_COLUMN in cols:
        return "msa"
    if "place" in cols:
        return "place"
    if "county" in cols:
        return "county"
    if "state" in cols:
        return "state"
    if "us" in cols:
        return "us"
    return None


def build_geoid(components: Mapping[str, object], geo_level: str) -> str:
    """Build a standard Census GEOID string for one entity.

    Tract: SSCCCTTTTTT (11), County: SSCCC (5), Place: SSPPPPP (7),
    ZCTA: ZZZZZ (5), CBSA: MMMMM (5), State: SS (2), Nation: 'US'.
    """
    if geo_level == "us":
        return "US"
    parts = GEOID_COMPONENTS.get(geo_level)
    if not parts:
        raise ValueError(f"Unsupported geography level for GEOID: {geo_level}")
    out = []
    for column, width in parts:
        value = components.get(column)
        if value is None or str(value) == "" or str(value).lower() == "nan":
            raise KeyError(f"Missing '{column}' component for {geo_level} GEOID")
        out.append(str(value).split(".")[0].zfill(width))
    return "".join(out)


def add_geoid_column(df: "pd.DataFrame", geo_level: Optional[str] = None) -> "pd.DataFrame":
    """Insert a ``GEOID`` column (as first column) built from geography components.

    Falls back to the suffix of the API's ``GEO_ID`` (e.g. 1400000US36061000100)
    when components are unavailable.
    """
    import pandas as pd  # local import keeps geo lightweight

    out = df.copy()
    if out.empty:
        if "GEOID" not in out.columns:
            out.insert(0, "GEOID", pd.Series(dtype=str))
        return out
    level = geo_level or detect_geo_level(out.columns)
    geoids: Optional[pd.Series] = None
    if level:
        try:
            geoids = out.apply(lambda row: build_geoid(row, level), axis=1)
        except (KeyError, ValueError):
            geoids = None
    if geoids is None and "GEO_ID" in out.columns:
        geoids = out["GEO_ID"].astype(str).str.split("US").str[-1]
    if geoids is None:
        return out
    if "GEOID" in out.columns:
        out = out.drop(columns=["GEOID"])
    out.insert(0, "GEOID", geoids.astype(str))
    return out


# ---------------------------------------------------------------------------
# ZIP Code / ZCTA resolution (zip2fips.json)
# ---------------------------------------------------------------------------

_ZIP2FIPS: Dict[str, str] = {}


def load_zip2fips(path: Optional[str] = None, force_reload: bool = False) -> Dict[str, str]:
    """Load the ZIP -> 5-digit county FIPS crosswalk (cached)."""
    global _ZIP2FIPS
    if _ZIP2FIPS and not force_reload and path is None:
        return _ZIP2FIPS
    target = path or get_resource_path("zip2fips.json")
    data: Dict[str, str] = {}
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as f:
            data = {str(k).zfill(5): str(v).zfill(5) for k, v in json.load(f).items()}
    if path is None:
        _ZIP2FIPS = data
    return data


def resolve_zip(zip_code: str) -> Optional[Dict[str, str]]:
    """Resolve a 5-digit ZIP code to its (primary) state and county FIPS codes."""
    zip_code = str(zip_code).strip().zfill(5)
    fips = load_zip2fips().get(zip_code)
    if not fips:
        return None
    state = fips[:2]
    return {
        "zip_code": zip_code,
        "county_geoid": fips,
        "state_code": state,
        "county_code": fips[2:],
        "state_name": STATE_MAPPING.get(state, ""),
    }


def zctas_for_state(state_code: str, county_code: Optional[str] = None) -> List[str]:
    """ZIP codes whose primary county lies in the given state (and county).

    Note: ZIP codes are USPS delivery routes; ZCTAs are their Census areal
    approximations. PO-box-only ZIPs have no ZCTA and will return no data.
    """
    state_code = str(state_code).zfill(2)
    prefix = state_code + (str(county_code).zfill(3) if county_code else "")
    return sorted(z for z, f in load_zip2fips().items() if f.startswith(prefix))


def counties_for_state(state_code: str) -> List[str]:
    """3-digit county FIPS codes in a state known to the ZIP crosswalk."""
    state_code = str(state_code).zfill(2)
    return sorted({f[2:] for f in load_zip2fips().values() if f.startswith(state_code)})
