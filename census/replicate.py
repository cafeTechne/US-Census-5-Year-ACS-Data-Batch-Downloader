# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact variance from Census Variance Replicate Estimate (VRE) tables.

For a subset of ACS 5-year detailed tables the Census Bureau publishes, next to
each estimate and MOE, the standard error and 80 successive-difference
replicate estimates (``Var_Rep1`` ... ``Var_Rep80``). Any statistic f computed
from those tables gets its variance by evaluating f on the estimate and on each
replicate:

    Var(f) = 4/80 * sum_{r=1..80} (f_r - f)^2,  MOE90 = 1.645 * sqrt(Var)

(VRE documentation, formulas (1) and (2); see ``VRE_DOCUMENTATION``). Unlike
the approximation formulas this captures the covariance between components, so
sums of correlated cells, ratios, proportions, and cross-geography aggregates
are exact under the survey design.

Contents:

* ``VRE_LAYOUTS``: per-release registry of URLs, file formats, and column
  conventions, each fact with the URL it was confirmed from.
* ``vre_tables`` / ``vre_available`` / ``vre_availability``: which tables and
  geography levels Census publishes VREs for in a release.
* ``fetch_vre``: download (or read from ``.cache/vre/<year>/``) one VRE file and
  return a *replicate frame* (``REPLICATE_FRAME_COLUMNS``).
* ``replicate_sum``, ``replicate_difference``, ``replicate_ratio``,
  ``replicate_proportion``, ``replicate_apply``: exact derived statistics.
* ``verify_published_moe``: recompute every row's MOE from its replicates and
  compare with the published MOE (validation check F3).

Downloaded files are treated strictly as data: they are decompressed and parsed
as CSV, nothing in them is executed.

Caveats from the VRE documentation that this module implements:

* Controlled estimates (CME ``*****``) have no sampling error; their published
  MOE is 0 (or blank in 2010-2014) and their replicates may differ from the
  estimate by weight rounding, so they are excluded from the MOE self-check and
  a statistic built only from controlled cells is reported with MOE 0.
* Replicates may be negative or zero; they have no meaning on their own.
* A replicate ratio or percent with a zero replicate denominator is set to 0
  (documentation, Example 7).
* Zero replicate variance on an uncontrolled estimate does not mean zero error:
  Census assigns a model-based MOE (documentation, equations 3-5, which need
  internal average weights and k-values). Such results get MOE NaN and a note,
  except a sum whose estimate is 0, which uses the largest published MOE among
  its zero components (documentation, Example 5).
* The published MOE is the official value even when a replicate MOE differs.
"""

import gzip
import hashlib
import io
import json
import math
import os
import time
import warnings
import zipfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from census.geo import get_resource_path
from census.variance import (
    METHOD_REPLICATE,
    REPLICATE_COLUMNS,
    REPLICATE_COUNT,
    Z90,
    replicate_variance,
)

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

VRE_LANDING_URL = "https://www.census.gov/programs-surveys/acs/data/variance-tables.html"
VRE_BASE_URL = "https://www2.census.gov/programs-surveys/acs/replicate_estimates"
# Formula, controlled estimates, zero-variance and zero-denominator rules, and
# the 2020 GEOID change are all from this document (pages 3-4, 8-13, 15).
VRE_DOCUMENTATION = (
    f"{VRE_BASE_URL}/2024/documentation/5-year/2020-2024_Variance_Replicate_Table_Documentation.pdf"
)

FIRST_VRE_YEAR = 2014  # 2010-2014 is the first 5-year VRE release (no /2013/ directory)
LAST_KNOWN_VRE_YEAR = 2024

# Summary levels Census publishes VRE files for (Geography_List sheet of every
# VRE_Table_and_Geo_List_<year>.xlsx, 2014-2024: identical eleven levels).
SUMMARY_LEVELS: Dict[str, str] = {
    "010": "Nation",
    "040": "State",
    "050": "County",
    "060": "County Subdivision",
    "140": "Census Tract",
    "150": "Census Block Group",
    "160": "Place",
    "250": "American Indian Area/Alaska Native Area/Hawaiian Homeland",
    "310": "Metropolitan/Micropolitan Statistical Area",
    "500": "Congressional District",
    "860": "Zip Code Tabulation Area (ZCTA)",
}

# Toolkit geography names (census.geo) and common aliases -> summary level.
GEO_LEVEL_SUMLEVS: Dict[str, str] = {
    "us": "010",
    "nation": "010",
    "state": "040",
    "county": "050",
    "county_subdivision": "060",
    "cousub": "060",
    "tract": "140",
    "block_group": "150",
    "place": "160",
    "aiannh": "250",
    "msa": "310",
    "cbsa": "310",
    "congressional_district": "500",
    "cd": "500",
    "zcta": "860",
}

# Tract and block-group files are split by state (<TABLE>_<SS>.csv.*); every
# other summary level is one national file per table (directory listings of
# <year>/data/5-year/<sumlev>/ for 2014 and 2024).
PER_STATE_SUMLEVS = frozenset({"140", "150"})
# Summary levels whose GEOID begins with the 2-digit state FIPS code, so a
# national file can be filtered to one state.
STATE_NESTED_SUMLEVS = frozenset({"040", "050", "060", "140", "150", "160", "500"})

# State-file suffixes present for B19001 in 2014/150 and 2024/140 (50 states, DC, PR).
VRE_STATE_CODES: Tuple[str, ...] = (
    "01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 "
    "31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56 72"
).split()

CONTROLLED_CME = "*****"
USER_AGENT = "acs-batch-downloader/replicate (research toolkit; python-requests)"

# Columns of a replicate frame, in order (PLAN_ROUND_7.md section 4, B3).
REPLICATE_FRAME_COLUMNS: List[str] = ["GEOID", "NAME", "variable", "estimate", "moe"] + list(
    REPLICATE_COLUMNS
)
# Extra columns kept after the contract columns.
REPLICATE_FRAME_EXTRA_COLUMNS: List[str] = ["se_published", "controlled", "title"]

DERIVED_COLUMNS: List[str] = ["estimate", "moe", "se", "cv_percent", "method", "notes"]


# ---------------------------------------------------------------------------
# Release registry
# ---------------------------------------------------------------------------


def _doc_name(year: int) -> str:
    first = year - 4
    if year <= 2015:
        return f"{first}_{year}_Variance_Replicate_Tables_Documentation.pdf"
    if year <= 2019:
        return f"{first}-{year}_Variance_Replicate_Tables_Documentation.pdf"
    return f"{first}-{year}_Variance_Replicate_Table_Documentation.pdf"


# SHA-256 of the published table lists as downloaded on 2026-11-02.
_TABLE_GEO_LIST_SHA256: Dict[int, str] = {
    2014: "36cf48e9c519a93575571a84e609e7c559fc36bcbf8dd98183a1e942d030f47f",
    2015: "75c041a1e8cec304fa1e0ce5e26495ac0357267df6f31e181afeee5afff70490",
    2016: "375ec3439a12bc05b4a3c4bfeb5ea556d11a0efd79792464ad95a5cd84964b70",
    2017: "b3ef8b96fbe40f6da89572e8ae5846ebd6e5f816c0dda0cc1441500e5b255661",
    2018: "e15de5674b20b29d98caa933fb7942b9569c1303fd0c4bbd2f61197afbcfaf23",
    2019: "d518462010a2792209bd43aef2ee7fc7c98e39724b6bd1b4636e169d74469194",
    2020: "974513a636637e71fd06a63a480207fed03bd5a4d0c9f549de9f0dec4f48c87d",
    2021: "df94b5c0543f55915b0695e6d07e760603cc6a81dce7b1774bca85973d9f4710",
    2022: "79cc022602424db8aaf7a660ccedbc0ec1968c4154178ce366e8a3c31fb518c0",
    2023: "19acb4a7b0290913cab2b2911b3498c1f956ccfccfb57eb667679b3904273d25",
    2024: "2be498a144f5d1813cd9558c257251922c480d9b8b03629681a03647d4821f14",
}
_TABLE_LIST_CSV_SHA256: Dict[int, str] = {
    2018: "a5aaa8708f10d2b63e7d1a020c5dcca797852fed91d47b087299d3fb5a23ad2c",
    2019: "d5e3da78a0435ad1a9230f720944eb637c4b2355093a665efddd92e13bb67eda",
    2020: "8199941a8bb56d1439855420f4d1cdd039982143d05c3ce1544ab64d936b53f1",
    2021: "772eedce826cdb60de3db4cc9ed4337ffb21cd08aa1c736d303f6eb1c72118d4",
    2022: "d7545c0db72192a1838108eb958c6b95796cf3920fa55e7fd3f259ce2accdecc",
    2023: "d3d6d4826923e13ad4b1230e4ebcecb1a9ec288c7808036e48ac9cb352358c29",
    2024: "15aaf1c99ecf75ca0b4edea3e50cfd27b8ea64691ac71075279665d3dbc2fdf2",
}


def _layout(year: int) -> Dict[str, Any]:
    data_dir = f"{VRE_BASE_URL}/{year}/data/5-year"
    doc_dir = f"{VRE_BASE_URL}/{year}/documentation/5-year"
    gz = year <= 2018
    upper = year >= 2018
    variant = year >= 2020
    table_list_csv = f"{doc_dir}/VRE_TABLE_LIST_{year}.csv" if year >= 2018 else None
    return {
        "year": year,
        "release": f"{year - 4}-{year}",
        "page_url": f"https://www.census.gov/programs-surveys/acs/data/variance-tables.{year}.html",
        "data_dir": data_dir,
        # National file: <data_dir>/<sumlev>/<TABLE><ext>;
        # tract/block group: <data_dir>/<sumlev>/<TABLE>_<SS><ext>.
        "file_ext": ".csv.gz" if gz else ".csv.zip",
        # 2014-2018: single-member gzip CSV (served as application/x-tar, but the
        # payload is plain gzip). 2019+: zip holding one CSV, named either
        # "<TABLE>.csv" (2019, 2021) or "<sumlev>/<TABLE>.csv" (2020, 2022-2024).
        "compression": "gzip" if gz else "zip",
        "encoding": "latin-1",  # e.g. 0xF1 for n-tilde in "Dona Ana County" (2014, 2023 county files)
        # Header: TBLID,GEOID,NAME,ORDER,TITLE,<estimate>,<moe>,CME,SE,Var_Rep1..Var_Rep80
        "estimate_column": "ESTIMATE" if upper else "estimate",
        "moe_column": "MOE" if upper else "moe",
        "replicate_columns": [f"Var_Rep{i}" for i in range(1, REPLICATE_COUNT + 1)],
        # Two rows per table precede the data: table title and universe (GEOID empty).
        "title_rows": 2,
        # GEOID prefix: <sumlev>00US before 2020 (e.g. 05000US48201), and
        # <sumlev><variant 2><component 2>US from 2016-2020 on (0500000US48201).
        "geoid_prefix_example": "0500000US" if variant else "05000US",
        "order_to_variable": "{TBLID}_{ORDER:03d}",
        "table_list_url": table_list_csv,
        "table_list_csv_sha256": _TABLE_LIST_CSV_SHA256.get(year),
        "table_geo_list_url": f"{doc_dir}/VRE_Table_and_Geo_List_{year}.xlsx",
        "table_geo_list_sha256": _TABLE_GEO_LIST_SHA256.get(year),
        "documentation_url": f"{doc_dir}/{_doc_name(year)}",
        "sources": {
            "directory": f"{data_dir}/",
            "header_and_geoid": f"{data_dir}/040/B01003{'.csv.gz' if gz else '.csv.zip'}",
            "geoid_change": VRE_DOCUMENTATION + " (page 15)",
            "order_mapping": "B19001 Alabama rows in 2014 and 2023 match api.census.gov "
            "group(B19001) estimates and MOEs exactly with variable = TBLID_ORDER(3 digits)",
        },
    }


VRE_LAYOUTS: Dict[int, Dict[str, Any]] = {
    year: _layout(year) for year in range(FIRST_VRE_YEAR, LAST_KNOWN_VRE_YEAR + 1)
}


def _split(codes: str) -> List[str]:
    return codes.split()


# Tables published per release, transcribed from the Table_List sheet of
# VRE_Table_and_Geo_List_<year>.xlsx (and, for 2018+, identical to
# VRE_TABLE_LIST_<year>.csv). Release order is preserved.
# fmt: off
VRE_TABLE_LISTS: Dict[int, str] = {
    2014: (
        "B01001 B01003 B02001 B02005 B02006 B02007 B02008 B02009 B02010 B02011 B02012 B02013 "
        "B03001 B03002 B03003 B04006 B05001 B05002 B05005 B05005PR B05006 B05006PR B06003 "
        "B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 B10056 B10057 B11001 "
        "B11002 B11003 B11005 B11007 B11010 B12001 B13001 B13002 B14001 B15002 B16001 B16004 "
        "B16008 B17001 B17006 B17007 B17010 B17021 B18101 B19001 B19025 B19051 B19055 B19056 "
        "B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 B19127 B19201 B19214 B19313 "
        "B20001 B20003 B20005 B21001 B22001 B23001 B23008 B24080 B25002 B25003 B25004 B25008 "
        "B25014 B25017 B25024 B25034 B25038 B25040 B25041 B25043 B25044 B25048 B25052 B25063 "
        "B25070 B25075 B25087 B25091 B27001 B27002 B27003 B27010 B27011 C02003 C15010 C24010 "
        "C24030"
    ),
    2015: (
        "B01001 B01003 B02001 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 B02016 "
        "B03001 B03002 B03003 B04006 B05001 B05002 B05005 B05005PR B05006 B05006PR B06003 "
        "B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 B10056 B10057 B11001 "
        "B11002 B11003 B11005 B11007 B11010 B12001 B13002 B14001 B15002 B16001 B16004 B16008 "
        "B17001 B17006 B17007 B17010 B17021 B18101 B19001 B19025 B19051 B19055 B19056 B19057 "
        "B19059 B19061 B19065 B19066 B19067 B19069 B19101 B19127 B19201 B19214 B19313 B20001 "
        "B20003 B20005 B21001 B22001 B23001 B23008 B24080 B25002 B25003 B25004 B25008 B25014 "
        "B25017 B25024 B25034 B25038 B25040 B25041 B25043 B25044 B25048 B25052 B25063 B25070 "
        "B25075 B25087 B25091 B27001 B27002 B27003 B27010 B27011 C02003 C15010 C24010 C24030"
    ),
    2016: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05005 B05005PR B05006 "
        "B05006PR B06003 B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 B10056 "
        "B10057 B11001 B11002 B11003 B11005 B11007 B11010 B12001 B13002 B14001 B15002 C15010 "
        "B16001 C16001 B16004 B16008 B17001 B17006 B17007 B17010 B17021 B18101 B19001 B19025 "
        "B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 B19127 "
        "B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 C24010 C24030 "
        "B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 B25038 B25040 B25041 "
        "B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 B27001 B27002 B27003 "
        "B27010 B27011"
    ),
    2017: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06003 B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 "
        "B10056 B10057 B11001 B11002 B11003 B11005 B11007 B11010 B12001 B13002 B14001 B15002 "
        "C15010 B16001 C16001 B16004 B16008 B17001 B17006 B17007 B17010 B17021 B18101 B19001 "
        "B19025 B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 "
        "B19127 B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 C24010 "
        "C24030 B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 B25038 B25040 "
        "B25041 B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 B27001 B27002 "
        "B27003 B27010 B27011 B28001 B28002"
    ),
    2018: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06003 B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 "
        "B10056 B10057 B11001 B11002 B11003 B11005 B11007 B11010 B12001 B13002 B14001 B15002 "
        "C15010 B16001 C16001 B16004 B16008 B17001 B17006 B17007 B17010 B17021 B18101 B19001 "
        "B19025 B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 "
        "B19127 B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 B23025 "
        "C24010 C24030 B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 B25038 "
        "B25040 B25041 B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 B27001 "
        "B27002 B27003 B27010 B27011 B28001 B28002"
    ),
    2019: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06003 B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 "
        "B10056 B10057 B11001 B11002 B11003 B11005 B11007 B11010 B11012 B12001 B13002 B14001 "
        "B15002 C15010 B16001 C16001 B16004 B16008 B17001 B17006 B17007 B17010 B17021 B18101 "
        "B19001 B19025 B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 "
        "B19101 B19127 B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 "
        "B23025 C24010 C24030 B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 "
        "B25038 B25040 B25041 B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 "
        "B27001 B27002 B27003 B27010 B27011 B28001 B28002"
    ),
    2020: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06003 B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 "
        "B10056 B10057 B11001 B11002 B11003 B11005 B11007 B11010 B11012 B12001 B13002 B14001 "
        "B15002 C15010 B16001 C16001 B16004 B16008 B17001 B17006 B17007 B17010 B17021 B18101 "
        "B19001 B19025 B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 "
        "B19101 B19127 B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 "
        "B23025 C24010 C24030 B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 "
        "B25038 B25040 B25041 B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 "
        "B27001 B27002 B27003 B27010 B27011 B28001 B28002"
    ),
    2021: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06003 B06003PR B07003 B07003PR B08013 B08301 B08303 B09019 B10050 "
        "B10056 B10057 B11001 B11002 B11003 B11005 B11007 B11010 B11012 B12001 B13002 B14001 "
        "B15002 C15010 B16001 C16001 B16004 B16008 B17001 B17006 B17007 B17010 B17021 B18101 "
        "B19001 B19025 B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 "
        "B19101 B19127 B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 "
        "B23025 C24010 C24030 B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 "
        "B25038 B25040 B25041 B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 "
        "B27001 B27002 B27003 B27010 B27011 B28001 B28002"
    ),
    2022: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02014 B02015 "
        "B02016 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06001 B06001PR B06003 B06003PR B06009 B06009PR B07003 B07003PR "
        "B08013 B08301 B08303 B09001 B09019 B10050 B10056 B10057 B11001 B11002 B11003 B11005 "
        "B11007 B11010 B11012 B12001 B13002 B14001 B15002 C15010 B16001 C16001 B16004 B16005 "
        "B16008 B17001 C17002 B17006 B17007 B17010 B17010A B17010B B17010C B17010D B17010E "
        "B17010F B17010G B17010H B17010I B17021 B17024 B18101 B19001 B19025 B19051 B19055 "
        "B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 B19127 B19201 B19214 "
        "B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 B23025 C24010 C24030 B24080 "
        "B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 B25038 B25040 B25041 B25043 "
        "B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 B25106 B26001 B27001 B27002 "
        "B27003 B27010 B27011 B28001 B28002"
    ),
    2023: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02015 B02016 "
        "B02020 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 B05005 B05005PR "
        "B05006 B05006PR B06001 B06001PR B06003 B06003PR B06009 B06009PR B07003 B07003PR "
        "B08013 B08301 B08303 B09001 B09019 B10050 B10056 B10057 B11001 B11002 B11003 B11005 "
        "B11007 B11010 B11012 B12001 B13002 B14001 B15002 C15010 B16001 C16001 B16004 B16005 "
        "B16008 B17001 C17002 B17006 B17007 B17010 B17010A B17010B B17010C B17010D B17010E "
        "B17010F B17010G B17010H B17010I B17021 B17024 B18101 B19001 B19025 B19051 B19055 "
        "B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 B19127 B19201 B19214 "
        "B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 B23025 C24010 C24030 B24080 "
        "B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 B25038 B25040 B25041 B25043 "
        "B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 B25106 B26001 B27001 B27002 "
        "B27003 B27010 B27011 B28001 B28002"
    ),
    2024: (
        "B01001 B01003 B02001 C02003 B02008 B02009 B02010 B02011 B02012 B02013 B02015 B02016 "
        "B02020 C02022 C02023 B03001 B03002 B03003 B04006 B05001 B05001PR B05002 B05003 "
        "B05005 B05005PR B05006 B05006PR B06001 B06001PR B06003 B06003PR B06009 B06009PR "
        "B07003 B07003PR B08013 B08301 B08303 B09001 B09019 B10050 B10056 B10057 B11001 "
        "B11002 B11003 B11005 B11007 B11010 B11012 B12001 B13002 B14001 B15002 C15010 B16001 "
        "C16001 B16004 B16005 B16008 B17001 C17002 B17006 B17007 B17010 B17010A B17010B "
        "B17010C B17010D B17010E B17010F B17010G B17010H B17010I B17021 B17024 B18101 B19001 "
        "B19025 B19051 B19055 B19056 B19057 B19059 B19061 B19065 B19066 B19067 B19069 B19101 "
        "B19127 B19201 B19214 B19313 B20001 B20003 B20005 B21001 B22001 B23001 B23008 B23025 "
        "C24010 C24030 B24080 B25002 B25003 B25004 B25008 B25014 B25017 B25024 B25034 B25038 "
        "B25040 B25041 B25043 B25044 B25048 B25052 B25063 B25070 B25075 B25087 B25091 B25106 "
        "B26001 B27001 B27002 B27003 B27010 B27011 B28001 B28002"
    ),
}

# Summary levels a table is NOT published at, from the 'Notes' column ("Not
# Available at ... Summary Level(s)") of the same workbook. Spot-checked:
# 2024/150/B17001_01 and 2024/010/B05001PR are 404, 2017/050/B16001 is 404,
# 2014/050/B16001 and 2024/040/B05001PR exist.
VRE_TABLE_EXCLUSIONS: Dict[int, Dict[str, Tuple[str, ...]]] = {
    2014: {
        **dict.fromkeys(_split(
            "B02005 B02006 B02007 B03001 B04006 B05001 B05002 B05005 B05006 B06003 B07003 "
            "B08013 B10050 B10056 B10057 B11010 B13001 B13002 B14001 B16004 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
    },
    2015: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05005 B05006 B06003 B07003 "
            "B08013 B10050 B10056 B10057 B11010 B13002 B14001 B16004 B16008 B17001 B17006 "
            "B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
    },
    2016: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05005 B05006 B06003 B07003 "
            "B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16004 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2017: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16004 B16008 "
            "B17001 B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 "
            "B27003 B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2018: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2019: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2020: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2021: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06003PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2022: {
        **dict.fromkeys(_split(
            "B02014 B02015 B02016 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06001PR B06003PR B06009PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2023: {
        **dict.fromkeys(_split(
            "B02015 B02016 B02020 B03001 B04006 B05001 B05002 B05003 B05005 B05006 B06003 "
            "B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 B16008 B17001 "
            "B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 B27002 B27003 "
            "B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06001PR B06003PR B06009PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
    2024: {
        **dict.fromkeys(_split(
            "B02015 B02016 B02020 C02022 C02023 B03001 B04006 B05001 B05002 B05003 B05005 "
            "B05006 B06003 B07003 B08013 B10050 B10056 B10057 B11010 B13002 B14001 C16001 "
            "B16008 B17001 B17006 B17007 B18101 B20005 B22001 B23001 B25048 B25052 B27001 "
            "B27002 B27003 B27011"
        ), ('150',)),
        **dict.fromkeys(_split(
            "B05001PR B05005PR B05006PR B06001PR B06003PR B06009PR B07003PR"
        ), ('010', '150', '250')),
        **dict.fromkeys(_split(
            "B16001"
        ), ('050', '060', '140', '150', '160', '250', '310', '860')),
    },
}
# fmt: on


# ACS 1-year: no variance replicate tables are published. Checked 2026-11-03:
# every release directory under VRE_BASE_URL (2014-2024) holds only
# ``data/5-year/`` and ``documentation/5-year/``, and the landing page
# (VRE_LANDING_URL) lists 5-year tables only. 1-year sampling error must come
# from the published MOEs (or PUMS replicate weights, census.pums).
VRE_1YEAR_LAYOUTS: Dict[int, Dict[str, Any]] = {}
VRE_1YEAR_CHECKED = {
    "checked_on": "2026-11-03",
    "sources": [VRE_LANDING_URL] + [f"{VRE_BASE_URL}/{y}/data/" for y in range(2014, 2025)],
    "finding": "only 5-year/ subdirectories are published; no 1-year VRE tables",
}
VRE_PRODUCT_LAYOUTS: Dict[str, Dict[int, Dict[str, Any]]] = {
    "5-year": VRE_LAYOUTS,
    "1-year": VRE_1YEAR_LAYOUTS,
}


def vre_years(product: str = "5-year") -> List[int]:
    """Releases (period end years) with VRE tables in the registry for a product."""
    from census.api import normalize_product

    return sorted(VRE_PRODUCT_LAYOUTS[normalize_product(product)])


def check_vre_product(product: str) -> None:
    """Raise ValueError for a product without published VRE tables (1-year)."""
    from census.api import PRODUCT_1YR, normalize_product

    if normalize_product(product) == PRODUCT_1YR and not VRE_1YEAR_LAYOUTS:
        raise ValueError(
            "The Census Bureau publishes variance replicate tables only for ACS 5-year "
            f"releases ({VRE_LANDING_URL}); for 1-year estimates use the published MOEs "
            "or PUMS replicate weights."
        )


def _check_year(year: int) -> Dict[str, Any]:
    layout = VRE_LAYOUTS.get(int(year))
    if layout is None:
        raise ValueError(
            f"No VRE layout registered for {year}; known releases are "
            f"{FIRST_VRE_YEAR}-{LAST_KNOWN_VRE_YEAR}. Newer releases must be confirmed "
            f"from {VRE_LANDING_URL} and added to VRE_LAYOUTS."
        )
    return layout


def sumlev_for(geo_level: str) -> str:
    """Summary level code for a toolkit geography name or a 3-digit code."""
    key = str(geo_level).strip().lower()
    if key in GEO_LEVEL_SUMLEVS:
        return GEO_LEVEL_SUMLEVS[key]
    if key.isdigit() and key.zfill(3) in SUMMARY_LEVELS:
        return key.zfill(3)
    raise ValueError(
        f"Unsupported geo_level '{geo_level}' for VRE tables; use one of "
        f"{sorted(GEO_LEVEL_SUMLEVS)} or a summary level in {sorted(SUMMARY_LEVELS)}."
    )


# ---------------------------------------------------------------------------
# Cache and download
# ---------------------------------------------------------------------------


class VREDownloadError(RuntimeError):
    """A VRE file could not be downloaded or failed validation."""


def vre_cache_dir(year: Optional[int] = None) -> str:
    """$CENSUS_VRE_DIR or <repo>/.cache/vre, optionally with a year subdirectory."""
    base = os.getenv("CENSUS_VRE_DIR") or get_resource_path(os.path.join(".cache", "vre"))
    return os.path.join(base, str(year)) if year is not None else base


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sidecar_path(path: str) -> str:
    return path + ".json"


def _read_cached(path: str) -> Optional[bytes]:
    """Cached bytes if the file exists and matches its sidecar SHA-256."""
    sidecar = _sidecar_path(path)
    if not (os.path.exists(path) and os.path.exists(sidecar)):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    try:
        with open(sidecar, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return raw if meta.get("sha256") == _sha256(raw) else None


def _write_cached(path: str, raw: bytes, url: str, content_type: str) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)
    meta = {
        "source_url": url,
        "sha256": _sha256(raw),
        "size_bytes": len(raw),
        "content_type": content_type,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(_sidecar_path(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


def _download(url: str, session: Any = None, timeout: float = 300.0) -> Tuple[bytes, str]:
    import requests  # local import keeps the pure functions importable without network deps

    getter = session if session is not None else requests
    try:
        resp = getter.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:  # pragma: no cover - network failure
        raise VREDownloadError(f"Download failed for {url}: {exc}") from exc
    raw = resp.content or b""
    content_type = str(resp.headers.get("Content-Type", ""))
    if b"Request Rejected" in raw[:2000]:
        # census.gov's firewall answers rapid scripted requests with this page.
        raise VREDownloadError(
            f"census.gov rejected the request for {url} (firewall 'Request Rejected' page). "
            "Wait and retry later; do not retry in a tight loop."
        )
    if resp.status_code == 404:
        raise VREDownloadError(f"Not published (HTTP 404): {url}")
    if resp.status_code != 200:
        raise VREDownloadError(f"HTTP {resp.status_code} for {url}")
    return raw, content_type


def _file_name(table: str, sumlev: str, state_code: Optional[str], ext: str) -> str:
    if sumlev in PER_STATE_SUMLEVS:
        return f"{table}_{state_code}{ext}"
    return f"{table}{ext}"


def vre_url(table: str, year: int, geo_level: str, state_code: Optional[str] = None) -> str:
    """Download URL of one VRE file (state_code required for tract/block group)."""
    layout = _check_year(year)
    sumlev = sumlev_for(geo_level)
    table = table.strip().upper()
    if sumlev in PER_STATE_SUMLEVS:
        if not state_code:
            raise ValueError(f"Summary level {sumlev} VRE files are per state; pass state_code.")
        state_code = str(state_code).zfill(2)
    return f"{layout['data_dir']}/{sumlev}/{_file_name(table, sumlev, state_code, layout['file_ext'])}"


# Uncompressed-size guard against malformed archives (largest real VRE CSV is
# well under this; a tract B01001 file for California is ~47 MB zipped).
MAX_UNCOMPRESSED_BYTES = 4 * 1024**3


def _decompress(raw: bytes) -> bytes:
    """Return the CSV bytes from a gzip or single-member zip VRE download."""
    if raw[:2] == b"\x1f\x8b":
        out = gzip.decompress(raw)
    elif raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [i for i in zf.infolist() if i.filename.lower().endswith(".csv")]
            if len(members) != 1:
                raise VREDownloadError(
                    f"Expected one CSV in the VRE zip, found {[i.filename for i in zf.infolist()]}"
                )
            if members[0].file_size > MAX_UNCOMPRESSED_BYTES:
                raise VREDownloadError("VRE zip member is implausibly large; refusing to extract.")
            out = zf.read(members[0])
    else:
        out = raw
    if not out.lstrip(b"\xef\xbb\xbf").upper().startswith(b"TBLID,GEOID,"):
        raise VREDownloadError("File is not a VRE CSV (header does not start with TBLID,GEOID).")
    return out


# ---------------------------------------------------------------------------
# Table lists and availability
# ---------------------------------------------------------------------------


def _parse_table_list_csv(raw: bytes) -> List[str]:
    text = raw.decode("utf-8-sig", errors="replace")
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    col = next((c for c in df.columns if c.strip().upper() == "TBLID"), None)
    if col is None:
        raise VREDownloadError("Table list CSV has no TBLID column.")
    return [t.strip().upper() for t in df[col] if t.strip()]


def vre_tables(
    year: int,
    refresh: bool = False,
    session: Any = None,
    cache_dir: Optional[str] = None,
) -> List[str]:
    """Tables Census publishes VREs for in one release, in published order.

    By default returns the registry transcription of the release's table list
    (offline, deterministic). With ``refresh=True`` downloads the published
    ``VRE_TABLE_LIST_<year>.csv`` (2018+) into the VRE cache, records its
    SHA-256 in a sidecar, and returns it; a warning is raised when it no longer
    matches the registry. 2014-2017 lists are published only as .xlsx, so
    ``refresh`` has no effect there.
    """
    layout = _check_year(year)
    frozen = _split(VRE_TABLE_LISTS[int(year)])
    url = layout["table_list_url"]
    if not url:
        return frozen
    path = os.path.join(cache_dir or vre_cache_dir(year), os.path.basename(url))
    raw = None if refresh else _read_cached(path)
    if raw is None and refresh:
        raw, ctype = _download(url, session=session, timeout=60)
        _write_cached(path, raw, url, ctype)
    if raw is None:
        return frozen
    published = _parse_table_list_csv(raw)
    if published != frozen:
        warnings.warn(
            f"Published VRE table list for {year} differs from the registry "
            f"(added {sorted(set(published) - set(frozen))}, removed "
            f"{sorted(set(frozen) - set(published))}); using the published list.",
            stacklevel=2,
        )
    return published


def vre_availability(
    table: str, year: int, geo_level: str, product: str = "5-year"
) -> Dict[str, Any]:
    """Whether Census publishes a VRE file for (table, release, geography), and why.

    Offline: uses the registry (published table list plus the "Not Available at
    ... Summary Level" notes in the release's Table_and_Geo_List workbook).
    ``product='1-year'`` is always unavailable (no 1-year VRE tables exist).
    """
    table = str(table).strip().upper()
    out: Dict[str, Any] = {"table": table, "year": int(year), "geo_level": geo_level}
    try:
        check_vre_product(product)
    except ValueError as exc:
        out.update(available=False, reason=str(exc), product="1-year")
        return out
    layout = VRE_LAYOUTS.get(int(year))
    if layout is None:
        out.update(
            available=False,
            reason=f"No VRE release registered for {year} (known: {FIRST_VRE_YEAR}-{LAST_KNOWN_VRE_YEAR}).",
        )
        return out
    try:
        sumlev = sumlev_for(geo_level)
    except ValueError as exc:
        out.update(available=False, reason=str(exc))
        return out
    out["summary_level"] = sumlev
    out["per_state_files"] = sumlev in PER_STATE_SUMLEVS
    out["table_list_source"] = layout["table_list_url"] or layout["table_geo_list_url"]
    if table not in set(_split(VRE_TABLE_LISTS[int(year)])):
        out.update(available=False, reason=f"{table} is not in the {layout['release']} VRE table list.")
        return out
    excluded = VRE_TABLE_EXCLUSIONS.get(int(year), {}).get(table, ())
    if sumlev in excluded:
        out.update(
            available=False,
            reason=f"{table} is not published at summary level {sumlev} in {layout['release']} "
            f"(Table_and_Geo_List note; excluded levels {list(excluded)}).",
        )
        return out
    out.update(available=True, reason="")
    if sumlev not in PER_STATE_SUMLEVS:
        out["url"] = vre_url(table, year, sumlev)
    else:
        out["url_template"] = vre_url(table, year, sumlev, state_code="SS").replace("_SS.", "_{SS}.")
    return out


def vre_available(table: str, year: int, geo_level: str) -> bool:
    """True when Census publishes VREs for this table, release, and geography level."""
    return bool(vre_availability(table, year, geo_level)["available"])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def normalize_vre_geoid(geoid: str) -> str:
    """Strip the summary-level prefix ('0500000US48201' or '05000US48201' -> '48201').

    The nation ('0100000US' / '01000US') becomes 'US', matching census.geo.build_geoid.
    """
    s = str(geoid).strip()
    if "US" not in s:
        return s
    suffix = s.split("US", 1)[1]
    return suffix if suffix else "US"


def read_vre_csv(source: Union[str, bytes, io.IOBase], encoding: Optional[str] = None) -> pd.DataFrame:
    """Parse a VRE CSV (any release) into a replicate frame.

    ``source`` is a path (.csv, .csv.gz, or .csv.zip), raw bytes (compressed or
    not), or a binary file object.
    """
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    elif isinstance(source, str):
        with open(source, "rb") as f:
            raw = f.read()
    else:
        raw = source.read()
    data = _decompress(raw)
    if encoding is None:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    else:
        text = data.decode(encoding)
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    df.columns = [str(c).strip().upper() for c in df.columns]
    needed = ["TBLID", "GEOID", "NAME", "ORDER", "ESTIMATE", "MOE", "CME", "SE"] + [
        f"VAR_REP{i}" for i in range(1, REPLICATE_COUNT + 1)
    ]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise VREDownloadError(f"VRE CSV is missing columns {missing[:5]}")
    df = df[df["GEOID"].str.strip() != ""]  # drop table-title and universe rows

    def num(col: str) -> np.ndarray:
        return pd.to_numeric(df[col].str.replace(",", "", regex=False), errors="coerce").to_numpy(float)

    order = pd.to_numeric(df["ORDER"], errors="coerce")
    if order.isna().any():
        raise VREDownloadError("VRE CSV has a non-numeric ORDER value.")
    tblid = df["TBLID"].str.strip().str.upper()
    out = pd.DataFrame(
        {
            "GEOID": df["GEOID"].map(normalize_vre_geoid).to_numpy(),
            "NAME": df["NAME"].to_numpy(),
            "variable": (tblid + "_" + order.astype(int).map("{:03d}".format)).to_numpy(),
            "estimate": num("ESTIMATE"),
            "moe": num("MOE"),
        }
    )
    reps = np.column_stack([num(f"VAR_REP{i}") for i in range(1, REPLICATE_COUNT + 1)])
    out = pd.concat([out, pd.DataFrame(reps, columns=REPLICATE_COLUMNS)], axis=1)
    out["se_published"] = num("SE")
    out["controlled"] = (df["CME"].str.strip() == CONTROLLED_CME).to_numpy()
    out["title"] = df["TITLE"].to_numpy() if "TITLE" in df.columns else ""
    out.attrs["geoid_prefix"] = str(df["GEOID"].iloc[0]).split("US", 1)[0] + "US" if len(df) else ""
    return out.reset_index(drop=True)


def fetch_vre(
    table: str,
    year: int,
    geo_level: str,
    state_code: Optional[str] = None,
    client: Any = None,
    session: Any = None,
    cache_dir: Optional[str] = None,
    refresh: bool = False,
    check_moe: bool = True,
) -> pd.DataFrame:
    """Download (or read from cache) one VRE file and return its replicate frame.

    Tract and block-group files are per state, so ``state_code`` is required for
    them; for state-nested national files (state, county, county subdivision,
    place, congressional district) ``state_code`` filters the rows.

    Files are cached under ``$CENSUS_VRE_DIR`` (default ``.cache/vre``) as
    ``<year>/<sumlev>/<file>`` with a ``.json`` sidecar holding the source URL
    and SHA-256; a cached file whose hash does not match is downloaded again.
    ``client`` (a census.api client) is accepted for interface symmetry; only
    its ``session`` attribute is used, if present.

    With ``check_moe`` the published-MOE self-check runs on load and a warning
    is issued when any uncontrolled row disagrees beyond rounding.
    """
    table = str(table).strip().upper()
    info = vre_availability(table, year, geo_level)
    if not info["available"]:
        raise ValueError(f"VRE not available: {info['reason']}")
    sumlev = info["summary_level"]
    state = str(state_code).zfill(2) if state_code not in (None, "") else None
    if state is not None and sumlev not in STATE_NESTED_SUMLEVS:
        raise ValueError(f"Summary level {sumlev} is not nested in states; omit state_code.")
    url = vre_url(table, year, sumlev, state)
    path = os.path.join(cache_dir or vre_cache_dir(year), sumlev, url.rsplit("/", 1)[-1])
    raw = None if refresh else _read_cached(path)
    meta: Dict[str, Any]
    if raw is None:
        if session is None and client is not None:
            session = getattr(client, "session", None)
        raw, ctype = _download(url, session=session)
        _decompress(raw)  # validate before caching
        meta = _write_cached(path, raw, url, ctype)
    else:
        with open(_sidecar_path(path), encoding="utf-8") as f:
            meta = json.load(f)
    frame = read_vre_csv(raw)
    if state is not None and sumlev not in PER_STATE_SUMLEVS:
        frame = frame[frame["GEOID"].str.startswith(state)].reset_index(drop=True)
    frame.attrs.update(
        table=table,
        year=int(year),
        release=VRE_LAYOUTS[int(year)]["release"],
        summary_level=sumlev,
        state_code=state,
        source_url=url,
        sha256=meta.get("sha256"),
        cache_path=path,
    )
    if check_moe:
        report = verify_published_moe(frame)
        frame.attrs["moe_check"] = {k: v for k, v in report.items() if k != "mismatches"}
        if not report["passed"]:
            warnings.warn(
                f"{report['n_mismatch']} of {report['rows_checked']} rows in {url} have a replicate "
                f"MOE that differs from the published MOE beyond rounding (max diff "
                f"{report['max_abs_diff']:.3f}); the published MOE is official.",
                stacklevel=2,
            )
    return frame


# ---------------------------------------------------------------------------
# Self-check (validation F3)
# ---------------------------------------------------------------------------


def verify_published_moe(frame: pd.DataFrame, tolerance: float = 0.5) -> Dict[str, Any]:
    """Recompute each row's MOE from its replicates and compare with the published MOE.

    Published count MOEs are integers, so a correct replicate MOE is within 0.5
    of the published value (``tolerance``). Skipped and counted separately:

    * controlled rows (CME '*****'): published MOE 0, replicates may differ from
      the estimate by weight rounding (VRE documentation p. 11-12);
    * model-based rows: all 80 replicates equal the estimate (zero replicate
      variance), almost always zero counts, whose published MOE comes from the
      average-weight/k-value model (VRE documentation eqs. 3-5);
    * rows without a published MOE.

    Observed on 2014-2024 county, tract, ZCTA, place, state, and nation files:
    every remaining row matches within 0.5.
    """
    est = frame["estimate"].to_numpy(float)
    reps = frame[REPLICATE_COLUMNS].to_numpy(float)
    computed = Z90 * np.sqrt(replicate_variance(est, reps))
    published = frame["moe"].to_numpy(float)
    controlled = (
        frame["controlled"].to_numpy(bool) if "controlled" in frame else np.zeros(len(frame), bool)
    )
    model_based = ~controlled & np.all(reps == est[:, None], axis=1)
    usable = ~controlled & ~model_based & np.isfinite(published) & np.isfinite(computed)
    diff = np.abs(computed - published)
    bad = usable & (diff > tolerance + 1e-9)
    max_diff = float(diff[usable].max()) if usable.any() else math.nan
    cols = [c for c in ("GEOID", "NAME", "variable") if c in frame]
    mism = frame.loc[bad, cols].copy()
    mism["published_moe"] = published[bad]
    mism["replicate_moe"] = computed[bad]
    return {
        "rows": int(len(frame)),
        "rows_checked": int(usable.sum()),
        "controlled_rows": int(controlled.sum()),
        "model_based_rows": int(model_based.sum()),
        "rows_without_published_moe": int(
            (~controlled & ~model_based & ~np.isfinite(published)).sum()
        ),
        "max_abs_diff": max_diff,
        "tolerance": tolerance,
        "n_mismatch": int(bad.sum()),
        "passed": bool(not bad.any()),
        "mismatches": mism.head(50).to_dict(orient="records"),
    }


# ---------------------------------------------------------------------------
# Exact derived statistics
# ---------------------------------------------------------------------------

Variables = Union[str, Sequence[str]]


def _as_list(v: Optional[Variables]) -> List[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def _cube(
    frame: pd.DataFrame, variables: Sequence[str], geoids: Optional[Sequence[str]] = None
) -> Tuple[List[str], List[str], np.ndarray, np.ndarray, np.ndarray]:
    """Arrays for (geography x variable).

    Returns (geoids, names, values[81, G, V], moe[G, V], controlled[G, V]) where
    values[0] is the estimate and values[1:] are the 80 replicates.
    """
    variables = list(variables)
    if not variables:
        raise ValueError("At least one variable is required.")
    sub = frame[frame["variable"].isin(variables)]
    if geoids is None:
        geo_list = list(dict.fromkeys(sub["GEOID"]))
    else:
        geo_list = [str(g) for g in geoids]
    sub = sub[sub["GEOID"].isin(geo_list)]
    if sub.duplicated(["GEOID", "variable"]).any():
        raise ValueError("Replicate frame has duplicate (GEOID, variable) rows.")
    idx = pd.MultiIndex.from_product([geo_list, variables], names=["GEOID", "variable"])
    indexed = sub.set_index(["GEOID", "variable"]).reindex(idx)
    missing = indexed["estimate"].isna() & indexed[REPLICATE_COLUMNS[0]].isna()
    if missing.any():
        pairs = [f"{g}/{v}" for g, v in indexed.index[missing.to_numpy()][:5]]
        raise KeyError(f"Replicate frame lacks rows for {pairs}")
    g, v = len(geo_list), len(variables)
    cols = ["estimate"] + list(REPLICATE_COLUMNS)
    values = indexed[cols].to_numpy(float).reshape(g, v, REPLICATE_COUNT + 1).transpose(2, 0, 1)
    moe = indexed["moe"].to_numpy(float).reshape(g, v)
    ctrl = (
        indexed["controlled"].fillna(False).to_numpy(bool).reshape(g, v)
        if "controlled" in indexed
        else np.zeros((g, v), bool)
    )
    names = (
        indexed["NAME"].groupby(level=0, sort=False).first().reindex(geo_list).fillna("").tolist()
        if "NAME" in indexed
        else [""] * g
    )
    return geo_list, names, values, moe, ctrl


def _finish(
    stat: np.ndarray,
    controlled: np.ndarray,
    notes: List[List[str]],
    zero_fallback_moe: Optional[np.ndarray] = None,
) -> pd.DataFrame:
    """stat: (81, n) with estimate first. Returns DerivedEstimate-field columns."""
    est = stat[0]
    reps = stat[1:].T  # (n, 80)
    se = np.sqrt(replicate_variance(est, reps))
    moe = Z90 * se
    method = [METHOD_REPLICATE] * len(est)
    for i in range(len(est)):
        if not np.isfinite(est[i]):
            moe[i] = se[i] = math.nan
            continue
        if controlled[i]:
            moe[i] = se[i] = 0.0
            notes[i].append(
                "controlled: every component is a controlled estimate (CME '*****'), "
                "so the result has no sampling error (VRE documentation p. 11)"
            )
        elif se[i] == 0:
            if zero_fallback_moe is not None and est[i] == 0 and np.isfinite(zero_fallback_moe[i]):
                moe[i] = zero_fallback_moe[i]
                se[i] = moe[i] / Z90
                notes[i].append(
                    "zero_estimate: replicate variance is 0; MOE is the largest published MOE among "
                    "the zero components (VRE documentation, Example 5; may understate error)"
                )
            else:
                moe[i] = se[i] = math.nan
                notes[i].append(
                    "zero_replicate_variance: an uncontrolled estimate has zero replicate variance; "
                    "Census assigns a model-based MOE (VRE documentation eqs. 3-5, needs average "
                    "weights and k-values), so the MOE is left undefined"
                )
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where(est != 0, se / np.abs(est) * 100.0, np.nan)
    return pd.DataFrame(
        {"estimate": est, "moe": moe, "se": se, "cv_percent": cv, "method": method, "notes": notes}
    )


def _result(
    geo_list: List[str],
    names: List[str],
    across: bool,
    body: pd.DataFrame,
) -> pd.DataFrame:
    if across:
        head = pd.DataFrame({"GEOID": [",".join(geo_list)], "NAME": ["; ".join(n for n in names if n)]})
    else:
        head = pd.DataFrame({"GEOID": geo_list, "NAME": names})
    return pd.concat([head, body.reset_index(drop=True)], axis=1)


def _combine(values: np.ndarray, ctrl: np.ndarray, across: bool) -> Tuple[np.ndarray, np.ndarray]:
    """Sum variables (axis 2) and, if ``across``, geographies. values: (81, G, V)."""
    total = values.sum(axis=2)  # (81, G)
    c = ctrl.all(axis=1)  # (G,)
    if across:
        total = total.sum(axis=1, keepdims=True)
        c = np.array([bool(c.all())])
    return total, c


def replicate_sum(
    frame: pd.DataFrame,
    variables: Variables,
    geoids: Optional[Sequence[str]] = None,
    across_geographies: bool = False,
) -> pd.DataFrame:
    """Sum of variables within each geography (or over all ``geoids`` combined).

    Exact replicate MOE (VRE documentation, Examples 1 and 2).
    """
    vars_ = _as_list(variables)
    geo_list, names, values, moe, ctrl = _cube(frame, vars_, geoids)
    total, c = _combine(values, ctrl, across_geographies)
    est = values[0]
    zero_moe = np.where(est == 0, moe, np.nan)  # published MOEs of zero cells
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        fallback = np.nanmax(zero_moe, axis=1)
        if across_geographies:
            fallback = np.array([np.nanmax(zero_moe)]) if np.isfinite(zero_moe).any() else np.array([np.nan])
    label = f"sum of {len(vars_)} variable(s)"
    if across_geographies:
        label += f" over {len(geo_list)} geographies"
    notes = [[label] for _ in range(total.shape[1])]
    body = _finish(total, c, notes, zero_fallback_moe=fallback)
    return _result(geo_list, names, across_geographies, body)


def replicate_difference(
    frame: pd.DataFrame,
    minuend: Variables,
    subtrahend: Variables,
    geoids: Optional[Sequence[str]] = None,
    across_geographies: bool = False,
) -> pd.DataFrame:
    """(sum of ``minuend``) - (sum of ``subtrahend``), with exact replicate MOE."""
    a, b = _as_list(minuend), _as_list(subtrahend)
    geo_list, names, va, _, ca = _cube(frame, a, geoids)
    _, _, vb, _, cb = _cube(frame, b, geo_list)
    ta, ctrl_a = _combine(va, ca, across_geographies)
    tb, ctrl_b = _combine(vb, cb, across_geographies)
    notes = [[f"difference: {'+'.join(a)} minus {'+'.join(b)}"] for _ in range(ta.shape[1])]
    body = _finish(ta - tb, ctrl_a & ctrl_b, notes)
    return _result(geo_list, names, across_geographies, body)


def _ratio_stat(num: np.ndarray, den: np.ndarray, notes: List[List[str]]) -> np.ndarray:
    """num/den on (81, n); undefined replicate ratios become 0 (VRE doc, Example 7)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den != 0, num / den, np.nan)
    for i in range(out.shape[1]):
        if den[0, i] == 0:
            notes[i].append("zero_denominator: the estimate's denominator is 0, so the ratio is undefined")
            out[:, i] = np.nan
            continue
        bad = ~np.isfinite(out[1:, i])
        if bad.any():
            notes[i].append(
                f"zero_replicate_denominator: {int(bad.sum())} replicate ratio(s) undefined and set to 0 "
                "(VRE documentation, Example 7)"
            )
            out[1:, i][bad] = 0.0
    return out


def replicate_ratio(
    frame: pd.DataFrame,
    numerator: Variables,
    denominator: Variables,
    geoids: Optional[Sequence[str]] = None,
    across_geographies: bool = False,
    multiplier: float = 1.0,
) -> pd.DataFrame:
    """Ratio of summed numerator to summed denominator variables, exact replicate MOE.

    ``multiplier`` scales the estimate and MOE (100 for a percent).
    """
    nv, dv = _as_list(numerator), _as_list(denominator)
    geo_list, names, vn, _, cn = _cube(frame, nv, geoids)
    _, _, vd, _, cd = _cube(frame, dv, geo_list)
    tn, ctrl_n = _combine(vn, cn, across_geographies)
    td, ctrl_d = _combine(vd, cd, across_geographies)
    notes = [[f"ratio: {'+'.join(nv)} / {'+'.join(dv)}"] for _ in range(tn.shape[1])]
    stat = _ratio_stat(tn, td, notes) * multiplier
    body = _finish(stat, ctrl_n & ctrl_d, notes)
    return _result(geo_list, names, across_geographies, body)


def replicate_proportion(
    frame: pd.DataFrame,
    numerator: Variables,
    denominator: Variables,
    geoids: Optional[Sequence[str]] = None,
    across_geographies: bool = False,
    as_percent: bool = False,
) -> pd.DataFrame:
    """Proportion (numerator a subset of denominator), exact replicate MOE.

    The replicate method needs no subset/non-subset distinction; a note is added
    when an estimate's numerator exceeds its denominator, which signals that the
    numerator is not actually a subset.
    """
    out = replicate_ratio(
        frame, numerator, denominator, geoids, across_geographies, 100.0 if as_percent else 1.0
    )
    top = 100.0 if as_percent else 1.0
    for i, value in enumerate(out["estimate"]):
        if np.isfinite(value) and value > top + 1e-12:
            out.at[i, "notes"] = out.at[i, "notes"] + [
                "not_a_subset: numerator exceeds denominator; use replicate_ratio"
            ]
    return out


def replicate_apply(
    frame: pd.DataFrame,
    func: Callable[[Any], Any],
    variables: Optional[Variables] = None,
    geoids: Optional[Sequence[str]] = None,
    as_dict: bool = False,
    note: Optional[str] = None,
) -> pd.DataFrame:
    """Exact replicate MOE for any statistic of the replicate frame.

    ``func`` receives an array shaped (n_geographies, n_variables) - or, with
    ``as_dict=True``, a dict {variable: array(n_geographies)} - and returns a
    scalar or a 1-D array. It is evaluated on the published estimates and on
    each of the 80 replicate columns, then census.variance.replicate_moe
    formula is applied. ``variables`` defaults to every variable in the frame
    (in first-seen order); ``geoids`` to every geography.

    A result of length n_geographies is labelled with GEOID/NAME; a scalar gives
    one row labelled with all GEOIDs; any other length is labelled by position.
    Medians (census.medians) and aggregation (census.aggregate) build on this.
    """
    vars_ = _as_list(variables) or list(dict.fromkeys(frame["variable"]))
    geo_list, names, values, _, ctrl = _cube(frame, vars_, geoids)

    def call(mat: np.ndarray) -> np.ndarray:
        arg = {v: mat[:, j] for j, v in enumerate(vars_)} if as_dict else mat
        return np.atleast_1d(np.asarray(func(arg), dtype=float))

    results = [call(values[k]) for k in range(REPLICATE_COUNT + 1)]
    shapes = {r.shape for r in results}
    if len(shapes) != 1 or len(results[0].shape) != 1:
        raise ValueError("func must return a scalar or a 1-D array of the same length for every replicate.")
    stat = np.vstack(results)  # (81, n)
    n = stat.shape[1]
    base = [note] if note else ["replicate_apply"]
    notes = [list(base) for _ in range(n)]
    if n == len(geo_list):
        body = _finish(stat, ctrl.all(axis=1), notes)
        return _result(geo_list, names, False, body)
    body = _finish(stat, np.full(n, bool(ctrl.all())), notes)
    if n == 1:
        return _result(geo_list, names, True, body)
    body.insert(0, "index", range(n))
    return body


def replicate_frame_to_wide(frame: pd.DataFrame) -> pd.DataFrame:
    """Published estimates and MOEs as a wide frame (GEOID, NAME, <var>E, <var>M)."""
    est = frame.pivot(index="GEOID", columns="variable", values="estimate").add_suffix("E")
    moe = frame.pivot(index="GEOID", columns="variable", values="moe").add_suffix("M")
    names = frame.groupby("GEOID", sort=False)["NAME"].first()
    wide = pd.concat([names, est, moe], axis=1)
    order = [c for v in dict.fromkeys(frame["variable"]) for c in (f"{v}E", f"{v}M")]
    return wide[["NAME"] + order].reset_index()
