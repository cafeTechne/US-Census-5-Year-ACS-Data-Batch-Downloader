# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-vintage ACS variable and table metadata.

Source order for one (dataset, year):

1. ``metadata/<dataset>/<year>/variables.json.gz`` (offline archive, checksummed
   in ``metadata/manifest.json``; refresh with scripts/fetch_metadata_archive.py)
2. The Census API (``/{year}/{dataset}/variables.json``), cached in SQLite
3. Nothing: callers must treat the vintage as unverifiable

Every lookup reports which source it used, so extractions can record provenance.
"""

import gzip
import hashlib
import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from census.geo import get_resource_path

ARCHIVE_DIRNAME = "metadata"
DATASET_PATHS: Dict[str, str] = {
    "acs5": "acs/acs5",
    "profile": "acs/acs5/profile",
    "subject": "acs/acs5/subject",
    "cprofile": "acs/acs5/cprofile",
}

SOURCE_ARCHIVE = "archive"
SOURCE_API = "api"
SOURCE_MISSING = "missing"

VariableMeta = Dict[str, str]


def archive_dir() -> str:
    return os.getenv("CENSUS_METADATA_DIR") or get_resource_path(ARCHIVE_DIRNAME)


@lru_cache(maxsize=1)
def load_manifest() -> Dict[str, Any]:
    path = os.path.join(archive_dir(), "manifest.json")
    if not os.path.exists(path):
        return {"files": {}, "vintages": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def archived_vintages(dataset: str) -> List[int]:
    return [int(y) for y in load_manifest().get("vintages", {}).get(dataset, [])]


def available_vintages(dataset: str, client: Any = None, refresh: bool = False) -> List[int]:
    """Years published for a dataset.

    Uses the archive manifest; with ``refresh=True`` and a client, also reads the
    live Census API catalog so releases newer than the archive are included.
    """
    years = set(archived_vintages(dataset))
    if refresh and client is not None:
        catalog = client._get_json("https://api.census.gov/data.json", {})
        path = DATASET_PATHS.get(dataset)
        if isinstance(catalog, dict) and path:
            for entry in catalog.get("dataset", []):
                if "/".join(entry.get("c_dataset", [])) == path and entry.get("c_vintage"):
                    years.add(int(entry["c_vintage"]))
    return sorted(years)


def _archive_path(dataset: str, year: int, name: str) -> str:
    return os.path.join(archive_dir(), dataset, str(year), f"{name}.gz")


@lru_cache(maxsize=128)
def _read_archive(dataset: str, year: int, name: str) -> Optional[bytes]:
    path = _archive_path(dataset, year, name)
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rb") as f:
        return f.read()


def verify_archive_file(dataset: str, year: int, name: str = "variables.json") -> bool:
    """True if an archived file matches the SHA-256 recorded in the manifest."""
    raw = _read_archive(dataset, year, name)
    entry = load_manifest().get("files", {}).get(f"{dataset}/{year}/{name}.gz")
    if raw is None or not entry:
        return False
    return hashlib.sha256(raw).hexdigest() == entry.get("sha256")


def _normalize_variables(payload: Dict[str, Any]) -> Dict[str, VariableMeta]:
    out: Dict[str, VariableMeta] = {}
    for code, d in (payload.get("variables") or {}).items():
        if not isinstance(d, dict):
            continue
        out[code] = {
            "label": str(d.get("label", "")).strip(),
            "concept": str(d.get("concept", "")).strip(),
            "group": str(d.get("group", "")),
            "predicateType": str(d.get("predicateType", "")),
        }
    return out


_API_MEMO: Dict[Tuple[str, int, str], Dict[str, Any]] = {}


def _load_json(
    dataset: str, year: int, name: str, client: Any = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    raw = _read_archive(dataset, year, name)
    if raw is not None:
        return json.loads(raw), SOURCE_ARCHIVE
    key = (dataset, int(year), name)
    if key in _API_MEMO:
        return _API_MEMO[key], SOURCE_API
    if client is not None and dataset in DATASET_PATHS:
        url = f"{client.base_url}/{year}/{DATASET_PATHS[dataset]}/{name}"
        payload = client._get_json(url, {})
        if isinstance(payload, dict):
            _API_MEMO[key] = payload
            return payload, SOURCE_API
    return None, SOURCE_MISSING


@lru_cache(maxsize=64)
def _archived_variables(dataset: str, year: int) -> Optional[Dict[str, VariableMeta]]:
    raw = _read_archive(dataset, year, "variables.json")
    return _normalize_variables(json.loads(raw)) if raw is not None else None


def vintage_variables(
    dataset: str, year: int, client: Any = None
) -> Tuple[Dict[str, VariableMeta], str]:
    """All variables for one vintage and the source they came from."""
    archived = _archived_variables(dataset, int(year))
    if archived is not None:
        return archived, SOURCE_ARCHIVE
    payload, source = _load_json(dataset, int(year), "variables.json", client)
    return (_normalize_variables(payload) if payload else {}), source


def table_variables(
    table_code: str, dataset: str, year: int, client: Any = None
) -> Tuple[Dict[str, VariableMeta], str]:
    """Variables belonging to one table group in one vintage."""
    variables, source = vintage_variables(dataset, year, client)
    table = table_code.upper()
    selected = {
        code: meta
        for code, meta in variables.items()
        if meta.get("group") == table or (not meta.get("group") and code.startswith(table + "_"))
    }
    return selected, source


def vintage_groups(
    dataset: str, year: int, client: Any = None
) -> Tuple[Dict[str, Dict[str, str]], str]:
    """Table code -> {description, universe} for one vintage."""
    payload, source = _load_json(dataset, int(year), "groups.json", client)
    out: Dict[str, Dict[str, str]] = {}
    for item in (payload or {}).get("groups", []):
        universe = item.get("universe ") or item.get("universe") or ""
        out[item["name"]] = {
            "description": str(item.get("description", "")).strip(),
            "universe": str(universe).strip(),
        }
    return out, source


def table_universe(table_code: str, dataset: str, year: int, client: Any = None) -> str:
    """Readable table universe for one vintage ('' when not published).

    Some vintages (e.g. 2020) publish universe codes such as HSHLD; known codes
    are translated, unknown codes are returned unchanged.
    """
    from census.tables import UNIVERSE_NAMES  # local import avoids a cycle

    groups, _ = vintage_groups(dataset, year, client)
    raw = groups.get(table_code.upper(), {}).get("universe", "")
    return UNIVERSE_NAMES.get(raw, raw)


def latest_vintage(dataset: str) -> Optional[int]:
    years = archived_vintages(dataset)
    return max(years) if years else None


def describe_variable(
    code: str, year: Optional[int] = None, dataset: Optional[str] = None, client: Any = None
) -> Dict[str, Any]:
    """Label, concept, table and universe for one variable code in one vintage."""
    from census.api import infer_dataset  # local import avoids a cycle

    dataset = dataset or infer_dataset(code)
    year = year or latest_vintage(dataset)
    if year is None:
        return {"found": False, "error": f"No vintages known for dataset '{dataset}'."}
    variables, source = vintage_variables(dataset, year, client)
    meta = variables.get(code)
    if meta is None:
        return {
            "found": False,
            "code": code,
            "year": year,
            "dataset": dataset,
            "metadata_source": source,
        }
    return {
        "found": True,
        "code": code,
        "year": year,
        "dataset": dataset,
        "label": meta["label"],
        "concept": meta["concept"],
        "table": meta["group"],
        "universe": table_universe(meta["group"], dataset, year, client),
        "predicate_type": meta["predicateType"],
        "metadata_source": source,
    }
