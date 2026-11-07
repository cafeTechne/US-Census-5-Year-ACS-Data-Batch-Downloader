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
from typing import Any, Dict, Iterator, Mapping, Optional

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
