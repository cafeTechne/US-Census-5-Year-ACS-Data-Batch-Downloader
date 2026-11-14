# SPDX-License-Identifier: AGPL-3.0-or-later
"""Table groups and variable definitions loader for US Census ACS.

Defaults read the latest vintage in the offline metadata archive
(metadata/<dataset>/<year>/*.json.gz). For multi-year work use
census.metadata, which resolves metadata per vintage.
"""

import gzip
import json
import os
from typing import Any, Dict, Optional

from census.metadata import archive_dir, latest_vintage, read_archived_path
from census.metadata import clear_cache as clear_metadata_cache


def _read_json(path: str) -> Dict[str, Any]:
    archived = read_archived_path(path)
    if archived is not None:
        return archived
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[operator]
        return json.load(f)


def default_metadata_file(name: str, dataset: str = "acs5", year: Optional[int] = None) -> str:
    """Archive path of ``name`` (variables.json / groups.json) for a vintage.

    Defaults to the latest archived vintage of the dataset.
    """
    year = year or latest_vintage(dataset)
    return os.path.join(archive_dir(), *dataset.split("/"), str(year), f"{name}.gz")

VARIABLE_DEFINITIONS: Dict[str, str] = {}
_SOURCE_PATHS: Dict[str, str] = {}


def clear_cache() -> None:
    """Clear legacy lookup snapshots along with census.metadata.clear_cache()."""
    VARIABLE_DEFINITIONS.clear()
    VARIABLE_METADATA.clear()
    TABLE_UNIVERSES.clear()
    _SOURCE_PATHS.clear()



def load_tables_from_local(path: str) -> Dict[str, str]:
    """Load table groups mapping (table code -> description) from a groups.json(.gz)."""
    data = _read_json(path)
    tables = {item["name"]: item["description"] for item in data.get("groups", [])}
    for key, value in tables.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"Invalid structure for all_tables. Key: {key}, Value: {value}"
            )
    return tables


def load_variable_definitions(
    filepath: Optional[str] = None, force_reload: bool = False
) -> Dict[str, str]:
    """Load and cache variable definitions from variables.json."""
    global VARIABLE_DEFINITIONS
    if force_reload:
        clear_metadata_cache()
    target_path = filepath or default_metadata_file("variables.json")
    source_path = os.path.normcase(os.path.realpath(target_path))
    if VARIABLE_DEFINITIONS and _SOURCE_PATHS.get("VARIABLE_DEFINITIONS") == source_path:
        return VARIABLE_DEFINITIONS
    VARIABLE_DEFINITIONS = {}
    _SOURCE_PATHS["VARIABLE_DEFINITIONS"] = source_path
    if os.path.exists(target_path):
        data = _read_json(target_path)
        VARIABLE_DEFINITIONS = {
            var: str(details.get("label", "")).strip()
            for var, details in data.get("variables", {}).items()
        }
    return VARIABLE_DEFINITIONS


def get_variable_descriptions(
    table_code: str, definitions: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Retrieve variable descriptions starting with the given table code."""
    if not isinstance(table_code, str):
        raise ValueError(
            f"Expected table_code to be a string but got {type(table_code)}"
        )

    defs = definitions if definitions is not None else load_variable_definitions()
    return {var: label for var, label in defs.items() if var.startswith(table_code)}


# ---------------------------------------------------------------------------
# Curated non-detailed ACS 5-year tables
# ---------------------------------------------------------------------------

DATA_PROFILE_TABLES: Dict[str, str] = {
    "DP02": "SELECTED SOCIAL CHARACTERISTICS IN THE UNITED STATES",
    "DP03": "SELECTED ECONOMIC CHARACTERISTICS",
    "DP04": "SELECTED HOUSING CHARACTERISTICS",
    "DP05": "ACS DEMOGRAPHIC AND HOUSING ESTIMATES",
}

SUBJECT_TABLES: Dict[str, str] = {
    "S0101": "AGE AND SEX",
    "S1101": "HOUSEHOLDS AND FAMILIES",
    "S1501": "EDUCATIONAL ATTAINMENT",
    "S1701": "POVERTY STATUS IN THE PAST 12 MONTHS",
    "S1801": "DISABILITY CHARACTERISTICS",
    "S1901": "INCOME IN THE PAST 12 MONTHS (IN INFLATION-ADJUSTED DOLLARS)",
    "S2301": "EMPLOYMENT STATUS",
    "S2501": "OCCUPANCY CHARACTERISTICS",
    "S2701": "SELECTED CHARACTERISTICS OF HEALTH INSURANCE COVERAGE IN THE UNITED STATES",
    "S2801": "TYPES OF COMPUTERS AND INTERNET SUBSCRIPTIONS",
}

COMPARISON_PROFILE_TABLES: Dict[str, str] = {
    "CP02": "COMPARATIVE SOCIAL CHARACTERISTICS IN THE U.S.",
    "CP03": "COMPARATIVE ECONOMIC CHARACTERISTICS",
    "CP04": "COMPARATIVE HOUSING CHARACTERISTICS",
    "CP05": "COMPARATIVE DEMOGRAPHIC ESTIMATES",
}

DATASET_TABLES: Dict[str, Dict[str, str]] = {
    "profile": DATA_PROFILE_TABLES,
    "subject": SUBJECT_TABLES,
    "cprofile": COMPARISON_PROFILE_TABLES,
}

# groups.json universe codes -> readable universe names
UNIVERSE_NAMES: Dict[str, str] = {
    "TOTAL_POP": "Total population",
    "HSHLD": "Households",
    "FAMILY": "Families",
    "HOUSING_UNIT": "Housing units",
    "OCCUPIED_UNIT": "Occupied housing units",
    "OWNER_UNIT": "Owner-occupied housing units",
    "RENTER_OCCUNIT": "Renter-occupied housing units",
    "RENTER_OCCUPIED_UNIT_CASH": "Renter-occupied housing units paying cash rent",
    "NONINST": "Civilian noninstitutionalized population",
    "WRKR_16_OVER": "Workers 16 years and over",
    "WRKR_16_OVER_NOHOME": "Workers 16 years and over who did not work from home",
    "POP_15_OVER": "Population 15 years and over",
    "POP_16_OVER": "Population 16 years and over",
    "POP_05_OVER": "Population 5 years and over",
    "POP_01_OVER_US": "Population 1 year and over in the United States",
}

VARIABLE_METADATA: Dict[str, Dict[str, str]] = {}
TABLE_UNIVERSES: Dict[str, str] = {}


def load_variable_metadata(
    filepath: Optional[str] = None, force_reload: bool = False
) -> Dict[str, Dict[str, str]]:
    """Load full variable metadata (label, concept, group, predicateType)."""
    global VARIABLE_METADATA
    if force_reload:
        clear_metadata_cache()
    target_path = filepath or default_metadata_file("variables.json")
    source_path = os.path.normcase(os.path.realpath(target_path))
    if VARIABLE_METADATA and _SOURCE_PATHS.get("VARIABLE_METADATA") == source_path:
        return VARIABLE_METADATA
    VARIABLE_METADATA = {}
    _SOURCE_PATHS["VARIABLE_METADATA"] = source_path
    if os.path.exists(target_path):
        data = _read_json(target_path)
        VARIABLE_METADATA = {
            var: {
                "label": str(d.get("label", "")),
                "concept": str(d.get("concept", "")),
                "group": str(d.get("group", "")),
                "predicateType": str(d.get("predicateType", "")),
            }
            for var, d in data.get("variables", {}).items()
        }
    return VARIABLE_METADATA


def load_table_universes(
    filepath: Optional[str] = None, force_reload: bool = False
) -> Dict[str, str]:
    """Map table code -> readable universe, from groups.json."""
    global TABLE_UNIVERSES
    if force_reload:
        clear_metadata_cache()
    target_path = filepath or default_metadata_file("groups.json")
    source_path = os.path.normcase(os.path.realpath(target_path))
    if TABLE_UNIVERSES and _SOURCE_PATHS.get("TABLE_UNIVERSES") == source_path:
        return TABLE_UNIVERSES
    TABLE_UNIVERSES = {}
    _SOURCE_PATHS["TABLE_UNIVERSES"] = source_path
    if os.path.exists(target_path):
        data = _read_json(target_path)
        result: Dict[str, str] = {}
        for item in data.get("groups", []):
            # groups.json uses the key "universe " (with trailing space)
            raw = item.get("universe ") or item.get("universe") or ""
            raw = str(raw).strip()
            result[item["name"]] = UNIVERSE_NAMES.get(
                raw, raw.replace("_", " ").capitalize()
            )
        TABLE_UNIVERSES = result
    return TABLE_UNIVERSES


def table_code_of(variable_code: str) -> str:
    """'B19013_001E' -> 'B19013'; 'S1701_C01_001E' -> 'S1701'."""
    return variable_code.split("_", 1)[0]


def load_table_catalog(dataset: str = "acs5", year: Optional[int] = None) -> Dict[str, str]:
    """Table code -> description for one vintage (default: latest archived)."""
    path = default_metadata_file("groups.json", dataset, year)
    if not os.path.exists(path):
        return {}
    return load_tables_from_local(path)
