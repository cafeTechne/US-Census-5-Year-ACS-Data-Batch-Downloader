# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load-time SHA-256 checks for bundled reference tables (decision D13-4, risk R1).

Bundled tables (data/price_indexes/*.csv, data/price_parities/*.csv) carry the
SHA-256 of their exact bytes in a sidecar JSON. When a table is read from the
package's own data directory, :func:`read_table_bytes` hashes the bytes it is
about to parse and raises :class:`BundledDataIntegrityError` on a mismatch.
A successful check is cached per file (path, size, modification time) for the
life of the process, so repeated loads do not re-hash.

Tables from a caller-supplied directory are not checked against anything; the
caller gets their SHA-256 back to record in the result's provenance.
"""

import hashlib
import os
from typing import Dict, Optional, Tuple

_VERIFIED: Dict[Tuple[str, int, int, str], bool] = {}


class BundledDataIntegrityError(ValueError):
    """A bundled reference table does not match the checksum shipped with it."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def same_directory(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def clear_verification_cache() -> None:
    _VERIFIED.clear()


def _key(path: str, expected: str) -> Tuple[str, int, int, str]:
    st = os.stat(path)
    return (os.path.normcase(os.path.realpath(path)), st.st_size, st.st_mtime_ns, expected)


def is_verified(path: str, expected: Optional[str]) -> bool:
    return bool(expected) and _key(path, expected) in _VERIFIED  # type: ignore[arg-type]


def verify_bundled_bytes(path: str, raw: bytes, expected: Optional[str], sidecar: str, update_script: str) -> None:
    """Raise unless ``raw`` (the bytes of bundled ``path``) hashes to ``expected``."""
    if expected and is_verified(path, expected):
        return
    actual = sha256_bytes(raw)
    if not expected or actual != expected:
        raise BundledDataIntegrityError(
            f"Bundled data file {path} failed its integrity check: expected SHA-256 "
            f"{expected or '(none recorded in ' + os.path.basename(sidecar) + ')'}, actual {actual}. "
            "The bundled data was modified or corrupted after it was generated. Restore it with "
            f"`git checkout -- {os.path.dirname(path)}` (or reinstall the package), or regenerate the "
            f"table and its checksum with `python {update_script}`."
        )
    _VERIFIED[_key(path, expected)] = True


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
