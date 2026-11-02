"""Geographic reference data and place parsing utilities for US Census ACS."""

import os
import sys
from typing import Dict, List, Optional, Tuple


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
