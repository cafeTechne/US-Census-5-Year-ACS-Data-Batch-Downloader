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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

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

    @staticmethod
    def _row(endpoint: str, params: Mapping[str, Any], payload: Any, now: float) -> Tuple[str, str, str, str, float]:
        clean = {k: v for k, v in params.items() if k != "key"}
        return (make_cache_key(endpoint, params), endpoint, json.dumps(clean, sort_keys=True),
                json.dumps(payload), now)

    def set(self, endpoint: str, params: Mapping[str, Any], payload: Any) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?)",
                         self._row(endpoint, params, payload, time.time()))

    def mark_synthetic(self, description: Optional[str] = None, generator: Optional[str] = None) -> None:
        """Mark every response in this cache as SYNTHETIC (e.g. frozen generated fixtures).

        Results built from a client using this cache are labelled synthetic
        (census.manifest.run_manifest), whatever a manifest declares.
        """
        value = json.dumps({"description": description, "generator": generator}, sort_keys=True)
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (SYNTHETIC_KEY, value))

    def write_frozen(self, entries: List[Tuple[str, Mapping[str, Any], Any]],
                     synthetic: Optional[Mapping[str, Any]] = None) -> None:
        """Write ``entries`` ((endpoint, params, payload)) and, when given, the synthetic
        marker ({description, generator}) in one transaction: all of it or nothing."""
        now = time.time()
        rows = [self._row(endpoint, params, payload, now) for endpoint, params, payload in entries]
        with self._connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?)", rows)
            if synthetic is not None:
                value = json.dumps({"description": synthetic.get("description"),
                                    "generator": synthetic.get("generator")}, sort_keys=True)
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (SYNTHETIC_KEY, value))

    def synthetic_marker(self) -> Optional[Dict[str, Any]]:
        """The synthetic marker ({description, generator}) or None for a real-data cache."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (SYNTHETIC_KEY,)).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self) -> int:
        """Delete every response and the synthetic marker (no synthetic response is left
        for it to describe). Returns the number of responses deleted; see
        :meth:`clear_all` for whether a marker was removed."""
        return self.clear_all()["cleared"]

    def clear_all(self) -> Dict[str, Any]:
        """:meth:`clear`, returning {"cleared": responses deleted, "synthetic_marker_cleared": bool}."""
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
            conn.execute("DELETE FROM responses")
            marked = conn.execute("DELETE FROM meta WHERE key = ?", (SYNTHETIC_KEY,)).rowcount
        return {"cleared": int(count), "synthetic_marker_cleared": bool(marked)}

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"path": self.path, "entries": int(count), "size_bytes": size}


def _frozen_docs(path: str) -> List[Tuple[str, bytes, Any]]:
    """(path, raw bytes, parsed JSON) of the frozen response files at ``path``: the file
    itself, or every ``*.json`` file in a directory that has a ``requests`` mapping
    (sorted by name). Each file is read and parsed once."""
    if not os.path.isdir(path):
        with open(path, "rb") as f:
            raw = f.read()
        try:
            return [(path, raw, json.loads(raw.decode("utf-8")))]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: not valid JSON ({exc})") from None
    found: List[Tuple[str, bytes, Any]] = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if name.endswith(".json") and os.path.isfile(full):
            with open(full, "rb") as f:
                raw = f.read()
            try:
                doc = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("requests"), dict):
                found.append((full, raw, doc))
    if not found:
        raise ValueError(f"No frozen response files (*.json with 'requests') in {path}")
    return found


def frozen_sources(path: str) -> List[str]:
    """The frozen response files at ``path``: the file itself, or every ``*.json``
    file in a directory that has a ``requests`` mapping (sorted by name)."""
    return [p for p, _, _ in _frozen_docs(path)]


def read_frozen(path: str) -> List[Dict[str, Any]]:
    """Parse and validate every frozen response file at ``path`` without writing
    anything: each item has ``path``, ``sha256``, ``tables``, ``synthetic``
    (the marker dict, or None) and ``entries`` ((endpoint, params, payload) under
    the exact request keys an extraction will use). Raises ValueError naming the
    first bad file."""
    from census.api import CensusAPIClient

    client = CensusAPIClient(api_key="", cache=None)
    out = []
    for source, raw, doc in _frozen_docs(path):
        request = doc.get("request") if isinstance(doc, dict) else None
        responses = doc.get("requests") if isinstance(doc, dict) else None
        if not isinstance(request, dict) or not isinstance(responses, dict) or not responses:
            raise ValueError(f"{source}: a frozen file needs a 'request' object and a non-empty 'requests' mapping")
        try:
            keys = [(table, client.build_request(table_code=table, **request)) for table in responses]
        except TypeError as exc:
            raise ValueError(f"{source}: bad 'request' arguments: {exc}") from None
        synthetic = doc.get("synthetic")
        out.append({"path": source, "sha256": hashlib.sha256(raw).hexdigest(), "tables": sorted(responses),
                    "synthetic": (synthetic if isinstance(synthetic, dict) else {}) if synthetic else None,
                    "entries": [(endpoint, params, responses[table]) for table, (endpoint, params) in keys]})
    return out


def import_frozen(cache: QueryCache, path: Union[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Load frozen API responses into ``cache`` under the exact request keys an
    extraction will use, so later runs read them instead of the Census API.

    A frozen file is JSON: ``request`` (the arguments of
    ``CensusAPIClient.build_request`` other than ``table_code``: year,
    geo_level, state_code, county_code, dataset, ...), ``requests``
    ({table_code: API response rows}) and, for generated data, ``synthetic``
    ({description, generator}). Any synthetic source marks the whole cache
    synthetic (:meth:`QueryCache.mark_synthetic`), so every result built from
    it is labelled synthetic; a real-data import never removes a marker (only
    ``cache clear``, :meth:`QueryCache.clear`, does, with the responses).

    Atomic: every file is parsed and validated first (``path`` may also be the
    result of :func:`read_frozen`), then all responses and the marker are
    written in one transaction; on any error nothing is written.
    """
    frozen = read_frozen(path) if isinstance(path, str) else path
    marker = next((f["synthetic"] for f in reversed(frozen) if f["synthetic"] is not None), None)
    cache.write_frozen([e for f in frozen for e in f["entries"]], synthetic=marker)
    sources = [{"path": f["path"], "sha256": f["sha256"], "tables": f["tables"],
                "synthetic": f["synthetic"] is not None} for f in frozen]
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
