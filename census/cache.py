# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite-backed local cache for Census API responses.

Responses are keyed by the request endpoint and query parameters (excluding the
API key), so re-running an analysis reproduces identical data offline and does
not spend Census API rate limit.
"""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional

from census.geo import get_resource_path

DEFAULT_CACHE_RELATIVE_PATH = os.path.join(".cache", "census_cache.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    cache_key   TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    params      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
)
"""
_META_SCHEMA = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
SYNTHETIC_KEY = "synthetic_source"


def make_cache_key(endpoint: str, params: Mapping[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if k != "key"}
    raw = endpoint + "?" + json.dumps(clean, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class QueryCache:
    """Small thread-safe (connection-per-call) SQLite response cache."""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_META_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Open/close per call: safe across threads and releases the file handle.
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            with conn:  # commit or roll back
                yield conn
        finally:
            conn.close()

    def get(self, endpoint: str, params: Mapping[str, Any]) -> Optional[Any]:
        key = make_cache_key(endpoint, params)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM responses WHERE cache_key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, endpoint: str, params: Mapping[str, Any], payload: Any) -> None:
        key = make_cache_key(endpoint, params)
        clean = {k: v for k, v in params.items() if k != "key"}
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    endpoint,
                    json.dumps(clean, sort_keys=True),
                    json.dumps(payload),
                    time.time(),
                ),
            )

    def mark_synthetic(self, description: Optional[str] = None, generator: Optional[str] = None) -> None:
        """Mark every response in this cache as SYNTHETIC (e.g. frozen generated fixtures).

        Results built from a client using this cache are labelled synthetic
        (census.manifest.run_manifest), whatever a manifest declares.
        """
        value = json.dumps({"description": description, "generator": generator}, sort_keys=True)
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (SYNTHETIC_KEY, value))

    def synthetic_marker(self) -> Optional[Dict[str, Any]]:
        """The synthetic marker ({description, generator}) or None for a real-data cache."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (SYNTHETIC_KEY,)).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self) -> int:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
            conn.execute("DELETE FROM responses")
        return int(count)

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"path": self.path, "entries": int(count), "size_bytes": size}


def frozen_sources(path: str) -> List[str]:
    """The frozen response files at ``path``: the file itself, or every ``*.json``
    file in a directory that has a ``requests`` mapping (sorted by name)."""
    if not os.path.isdir(path):
        return [path]
    found: List[str] = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if name.endswith(".json") and os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                try:
                    doc = json.load(f)
                except json.JSONDecodeError:
                    continue
            if isinstance(doc, dict) and isinstance(doc.get("requests"), dict):
                found.append(full)
    if not found:
        raise ValueError(f"No frozen response files (*.json with 'requests') in {path}")
    return found


def import_frozen(cache: QueryCache, path: str) -> Dict[str, Any]:
    """Load frozen API responses into ``cache`` under the exact request keys an
    extraction will use, so later runs read them instead of the Census API.

    A frozen file is JSON: ``request`` (the arguments of
    ``CensusAPIClient.build_request`` other than ``table_code``: year,
    geo_level, state_code, county_code, dataset, ...), ``requests``
    ({table_code: API response rows}) and, for generated data, ``synthetic``
    ({description, generator}). Any synthetic source marks the whole cache
    synthetic (:meth:`QueryCache.mark_synthetic`), so every result built from
    it is labelled synthetic; a real-data import never removes a marker.
    """
    from census.api import CensusAPIClient

    client = CensusAPIClient(api_key="", cache=cache)
    sources = []
    for source in frozen_sources(path):
        with open(source, "rb") as f:
            raw = f.read()
        doc = json.loads(raw.decode("utf-8"))
        request, responses = doc.get("request"), doc.get("requests")
        if not isinstance(request, dict) or not isinstance(responses, dict) or not responses:
            raise ValueError(f"{source}: a frozen file needs a 'request' object and a non-empty 'requests' mapping")
        try:
            keys = [(table, client.build_request(table_code=table, **request)) for table in responses]
        except TypeError as exc:
            raise ValueError(f"{source}: bad 'request' arguments: {exc}") from None
        for table, (endpoint, params) in keys:
            cache.set(endpoint, params, responses[table])
        synthetic = doc.get("synthetic")
        if synthetic:
            marker = synthetic if isinstance(synthetic, dict) else {}
            cache.mark_synthetic(description=marker.get("description"), generator=marker.get("generator"))
        sources.append({"path": source, "sha256": hashlib.sha256(raw).hexdigest(),
                        "tables": sorted(responses), "synthetic": bool(synthetic)})
    return {"imported": sum(len(s["tables"]) for s in sources), "sources": sources,
            "synthetic": cache.synthetic_marker() is not None}


def default_cache_path() -> str:
    """Cache location: $CENSUS_CACHE_PATH or <workspace>/.cache/census_cache.db."""
    return os.getenv("CENSUS_CACHE_PATH") or get_resource_path(
        DEFAULT_CACHE_RELATIVE_PATH
    )


def get_default_cache() -> Optional[QueryCache]:
    """Return the default cache, or None if disabled via CENSUS_CACHE_DISABLED=1."""
    if os.getenv("CENSUS_CACHE_DISABLED", "").strip() in ("1", "true", "yes"):
        return None
    try:
        return QueryCache(default_cache_path())
    except (OSError, sqlite3.Error):
        return None
