"""Table groups and variable definitions loader for US Census ACS."""

import json
import os
from typing import Dict, Optional
from census.geo import get_resource_path

VARIABLE_DEFINITIONS: Dict[str, str] = {}


def load_tables_from_local(path: str) -> Dict[str, str]:
    """Load table groups mapping (table code -> description) from groups.json."""
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
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
    if VARIABLE_DEFINITIONS and not force_reload:
        return VARIABLE_DEFINITIONS

    target_path = filepath or get_resource_path("variables.json")
    if os.path.exists(target_path):
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            VARIABLE_DEFINITIONS = {
                var: details["label"]
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
