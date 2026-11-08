# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load-time SHA-256 checks for bundled reference tables (decision D13-4, risk R1).

Bundled tables (data/price_indexes/*.csv, data/price_parities/*.csv) carry the
SHA-256 of their exact bytes in a sidecar JSON. When a table is read from the
package's own data directory, :func:`read_table_bytes` hashes the bytes it is
about to parse and raises :class:`BundledDataIntegrityError` on a mismatch.
The loaders (census.inflation, census.rpp) cache each parsed table for the
life of the process, so a file is read and hashed once; clearing their caches
(``load_price_index.cache_clear()``, ``rpp.clear_cache()``) re-reads and
re-checks it.

Tables from a caller-supplied directory are not checked against anything; the
caller gets their SHA-256 back to record in the result's provenance.
"""

import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

from census import errors

INTEGRITY_NEXT_STEP = (
    "restore the bundled data (git checkout -- data/, or reinstall the package) or regenerate it with the "
    "update script named in the error, then call again; do not substitute other price values"
)


class BundledDataIntegrityError(ValueError):
    """A bundled reference table does not match the checksum shipped with it."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def same_directory(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def read_sidecar(path: str) -> Dict[str, Any]:
    """A table's sidecar JSON, or {} when it is missing."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def integrity_error_result(exc: BundledDataIntegrityError) -> Dict[str, Any]:
    """Structured failure (error_code BUNDLED_DATA_INTEGRITY, next_step) for tools and the CLI."""
    return errors.error_result(errors.BUNDLED_DATA_INTEGRITY, str(exc), summary=str(exc),
                               next_step=INTEGRITY_NEXT_STEP)


def verify_bundled_bytes(path: str, raw: bytes, expected: Optional[str], sidecar: str, update_script: str) -> None:
    """Raise unless ``raw`` (the bytes of bundled ``path``) hashes to ``expected``."""
    actual = sha256_bytes(raw)
    if not expected or actual != expected:
        raise BundledDataIntegrityError(
            f"Bundled data file {path} failed its integrity check: expected SHA-256 "
            f"{expected or '(none recorded in ' + os.path.basename(sidecar) + ')'}, actual {actual}. "
            "The bundled data was modified or corrupted after it was generated. Restore it with "
            f"`git checkout -- {os.path.dirname(path)}` (or reinstall the package), or regenerate the "
            f"table and its checksum with `python {update_script}`."
        )


def read_table_bytes(
    path: str, bundled: bool, expected: Optional[str], sidecar: str, update_script: str
) -> Tuple[bytes, Optional[str]]:
    """Bytes of a table file, plus its SHA-256 when it came from a custom directory.

    Bundled files are verified against ``expected`` (and the returned hash is
    None, since the sidecar already records it); custom files are not verified.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if bundled:
        verify_bundled_bytes(path, raw, expected, sidecar, update_script)
        return raw, None
    return raw, sha256_bytes(raw)
