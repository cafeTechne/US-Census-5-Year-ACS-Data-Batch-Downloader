# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-vintage ACS (and 2020 Decennial Census) variable and table metadata.

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
import zlib
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from census.geo import get_resource_path
from census.data_integrity import BundledDataIntegrityError, same_directory

ARCHIVE_DIRNAME = "metadata"
DATASET_PATHS: Dict[str, str] = {
    "acs5": "acs/acs5",
    "profile": "acs/acs5/profile",
    "subject": "acs/acs5/subject",
    "cprofile": "acs/acs5/cprofile",
    # 1-year releases archive under metadata/acs1/<year>/ and
    # metadata/acs1/<profile|subject|cprofile>/<year>/
    "acs1": "acs/acs1",
    "acs1/profile": "acs/acs1/profile",
    "acs1/subject": "acs/acs1/subject",
    "acs1/cprofile": "acs/acs1/cprofile",
    # 2020 Decennial Census (census.decennial), archived under metadata/dec/<pl|dhc>/2020/
    "dec/pl": "dec/pl",
    "dec/dhc": "dec/dhc",
}

SOURCE_ARCHIVE = "archive"
SOURCE_API = "api"
SOURCE_MISSING = "missing"

VariableMeta = Dict[str, str]


def archive_dir() -> str:
    return os.getenv("CENSUS_METADATA_DIR") or get_resource_path(ARCHIVE_DIRNAME)


class MetadataIntegrityError(BundledDataIntegrityError):
    """Present metadata cannot safely be consumed; never substitute API data."""

    next_step = (
        "restore the metadata archive and its manifest from a trusted checkout or reinstall; "
        "for custom metadata, repair the caller-supplied JSON/gzip files. Then call "
        "census.metadata.clear_cache() or restart before retrying"
    )


def _root() -> str:
    return os.path.normcase(os.path.realpath(archive_dir()))


@lru_cache(maxsize=8)
def _manifest_at(root: str) -> Dict[str, Any]:
    path = os.path.join(root, "manifest.json")
    if not os.path.exists(path):
        return {"files": {}, "vintages": {}}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("files", {}), dict)
            or not isinstance(payload.get("vintages", {}), dict)
        ):
            raise ValueError("expected a manifest object with files and vintages mappings")
        return payload
    except (OSError, ValueError) as exc:
        raise MetadataIntegrityError(f"Metadata manifest {path} is unreadable or malformed: {exc}") from exc


def load_manifest() -> Dict[str, Any]:
    """Manifest for the resolved active root, cached until clear_cache/restart."""
    return _manifest_at(_root())


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
    return os.path.join(_root(), *dataset.split("/"), str(year), f"{name}.gz")


@lru_cache(maxsize=128)
def _archive_at(root: str, dataset: str, year: int, name: str) -> Optional[tuple]:
    path = os.path.join(root, *dataset.split("/"), str(year), f"{name}.gz")
    if not os.path.exists(path):
        return None
    bundled = same_directory(root, get_resource_path(ARCHIVE_DIRNAME))
    try:
        with gzip.open(path, "rb") as f:
            raw = f.read()
    except (OSError, EOFError, zlib.error) as exc:
        raise MetadataIntegrityError(f"Metadata archive {path} cannot be decompressed: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    entry = _manifest_at(root).get("files", {}).get(f"{dataset}/{year}/{name}.gz", {})
    expected = entry.get("sha256") if isinstance(entry, dict) else None
    if bundled and (not expected or actual != expected):
        raise MetadataIntegrityError(
            f"Bundled metadata {path} failed verification against {root}/manifest.json: "
            f"expected SHA-256 {expected or '(none recorded)'}, actual {actual}."
        )
    try:
        payload = json.loads(raw)
        field, kind = ("variables", dict) if name == "variables.json" else ("groups", list)
        if not isinstance(payload, dict) or not isinstance(payload.get(field), kind):
            raise ValueError(f"expected an object with a {field} {kind.__name__}")
    except (ValueError, UnicodeError) as exc:
        raise MetadataIntegrityError(
            f"Metadata archive {path} contains malformed JSON metadata: {exc}"
        ) from exc
    provenance = {
        "sha256": actual,
        "expected_sha256": expected,
        "integrity": "verified" if bundled else "caller_supplied_unverified",
        "url": entry.get("url") if isinstance(entry, dict) else None,
    }
    return raw, payload, provenance


def _read_archive(dataset: str, year: int, name: str) -> Optional[bytes]:
    record = _archive_at(_root(), dataset, int(year), name)
    return record[0] if record is not None else None


def archive_provenance(dataset: str, year: int, name: str = "variables.json") -> Dict[str, Any]:
    """Actual decompressed-byte digest and integrity status of an archived source."""
    record = _archive_at(_root(), dataset, int(year), name)
    return dict(record[2]) if record is not None else {}


def verify_archive_file(dataset: str, year: int, name: str = "variables.json") -> bool:
    """Whether the archive matches its manifest (custom matches are not official verification)."""
    try:
        record = _archive_at(_root(), dataset, int(year), name)
    except MetadataIntegrityError:
        return False
    return bool(record and record[2]["expected_sha256"] == record[2]["sha256"])


def _normalize_variables(payload: Dict[str, Any]) -> Dict[str, VariableMeta]:
    out: Dict[str, VariableMeta] = {}
    for code, d in (payload.get("variables") or {}).items():
        if not isinstance(d, dict):
            continue
        out[code] = {
            # 2020 Census labels start with an empty part (" !!Total:"): drop it
            "label": str(d.get("label", "")).strip().removeprefix("!!"),
            "concept": str(d.get("concept", "")).strip(),
            "group": str(d.get("group", "")),
            "predicateType": str(d.get("predicateType", "")),
        }
    return out


_API_MEMO: Dict[tuple, Dict[str, Any]] = {}


def _load_json(
    dataset: str, year: int, name: str, client: Any = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    record = _archive_at(_root(), dataset, int(year), name)
    if record is not None:
        return record[1], SOURCE_ARCHIVE
    key = (_root(), getattr(client, "base_url", None), dataset, int(year), name)
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
def _archived_variables_at(
    root: str, dataset: str, year: int, digest: str
) -> Optional[Dict[str, VariableMeta]]:
    record = _archive_at(root, dataset, year, "variables.json")
    return _normalize_variables(record[1]) if record is not None else None


def vintage_variables(dataset: str, year: int, client: Any = None) -> Tuple[Dict[str, VariableMeta], str]:
    """All variables for one vintage and the source they came from."""
    root = _root()
    record = _archive_at(root, dataset, int(year), "variables.json")
    archived = _archived_variables_at(root, dataset, int(year), record[2]["sha256"]) if record else None
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


def vintage_groups(dataset: str, year: int, client: Any = None) -> Tuple[Dict[str, Dict[str, str]], str]:
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
    """Label, concept, table and universe for one variable code in one vintage.

    2020 Census count codes (``P12_002N``) default to the DHC (``dec/dhc``);
    pass ``dataset='dec/pl'`` for P.L. 94-171 codes. Decennial results carry
    an ``uncertainty`` statement (not sample-based; MOE undefined).
    """
    from census.api import infer_dataset  # local import avoids a cycle
    from census.decennial import DECENNIAL_DHC, UNCERTAINTY_LABEL, is_decennial
    from census.stats import parse_variable_code

    parsed = parse_variable_code(str(code))
    if dataset is None and parsed and parsed[1] in ("N", "NA"):
        dataset = DECENNIAL_DHC
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
        **({"uncertainty": UNCERTAINTY_LABEL} if is_decennial(dataset) else {}),
    }


def clear_cache() -> None:
    """Forget metadata snapshots after repair/configuration changes; no hot reload is promised."""
    _manifest_at.cache_clear()
    _archive_at.cache_clear()
    _archived_variables_at.cache_clear()
    _API_MEMO.clear()
    from census import tables

    tables.clear_cache()


# Preserve existing explicit cache-reset hooks while making a reset coherent.
load_manifest.cache_clear = clear_cache
_read_archive.cache_clear = clear_cache


def preflight_archives(dataset: str, years) -> None:
    """Check present variable/group archives before any metadata API fallback."""
    for year in years:
        for name in ("variables.json", "groups.json"):
            _archive_at(_root(), dataset, int(year), name)


def read_archived_path(path: str) -> Optional[Dict[str, Any]]:
    """Use the shared integrity seam for legacy loaders addressing archive files."""
    # Match the addressed archive location before resolving a linked payload file.
    # A symlink inside a bundled archive must still satisfy that archive's checksum.
    resolved = os.path.normcase(os.path.abspath(path))
    roots = (archive_dir(), get_resource_path(ARCHIVE_DIRNAME))
    for root in roots:
        try:
            parts = os.path.relpath(resolved, os.path.normcase(os.path.abspath(root))).split(os.sep)
        except ValueError:  # different drives on Windows
            continue
        if (
            len(parts) < 3
            or parts[0] == os.pardir
            or not parts[-2].isdigit()
            or parts[-1] not in ("variables.json.gz", "groups.json.gz")
        ):
            continue
        record = _archive_at(
            os.path.normcase(os.path.realpath(root)), "/".join(parts[:-2]), int(parts[-2]), parts[-1][:-3]
        )
        return record[1] if record else None
    return None
