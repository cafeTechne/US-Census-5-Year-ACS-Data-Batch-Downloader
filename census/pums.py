# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACS Public Use Microdata Sample (PUMS) with replicate-weight variance
(PLAN_ROUND_10.md, workstream A).

Files (checked with HTTP HEAD/GET on 2026-11-03):

* state files ``<PUMS_DATA_BASE>/<year>/<1-Year|5-Year>/csv_<p|h><st>.zip``,
  e.g. ``.../pums/2024/5-Year/csv_pwy.zip`` (Wyoming persons, 2020-2024); each
  zip holds one CSV (``psam_p56.csv``) and a README PDF.
* data dictionaries ``<PUMS_DOCS_BASE>/data_dict/PUMS_Data_Dictionary_<Y>.csv``
  (1-year, 2017+) and ``PUMS_Data_Dictionary_<Y-4>-<Y>.csv`` (5-year,
  2013-2017+). Rows are ``NAME,<var>,<C|N>,<len>,<description>`` and
  ``VAL,<var>,<C|N>,<len>,<low>,<high>,<label>``.

Downloads are cached under ``$CENSUS_PUMS_DIR`` (default ``.cache/pums``) as
``<year>/<1-Year|5-Year>/<file>`` with a ``.json`` sidecar (source URL,
SHA-256); a file whose hash does not match is downloaded again (decision 1:
full state files on demand). Only the requested columns are read, in chunks.

Weights: person ``PWGTP`` with replicates ``PWGTP1``-``PWGTP80``; housing
``WGTP`` with ``WGTP1``-``WGTP80``. Every statistic is computed with the full
weight and with each replicate weight, and

    Var(X) = 4/80 * sum_{r=1..80} (X_r - X)^2,   MOE90 = 1.645 * SE

(successive difference replication; PUMS Accuracy of the Data, "Calculating
Standard Errors Using Replicate Weights";
https://www2.census.gov/programs-surveys/acs/tech_docs/pums/accuracy/2019_2023AccuracyPUMS.pdf;
same formula as :mod:`census.variance`).

Medians: ``bins=None`` gives the weighted sample median (smallest value whose
cumulative weight reaches half the total; the midpoint of two values when the
half is hit exactly). With ``bins`` (bracket edges), the Census PUMS Accuracy
of the Data "Medians" procedure is followed: the weighted distribution over
the brackets is linearly interpolated (:func:`census.medians.median_from_distribution`).
Either way the SE comes from recomputing the median with each replicate weight.

Reliability (decision 2): estimates from fewer than ``MIN_UNWEIGHTED_N`` (30)
unweighted records or with CV above ``MAX_CV_PERCENT`` (30%) are flagged,
never suppressed. Outputs are aggregates only.
"""

import csv
import hashlib
import io
import json
import math
import os
import re
import time
import warnings
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from .api import PRODUCT_1YR, PRODUCT_5YR, normalize_product
from .geo import STATE_FILES, STATE_MAPPING, get_resource_path
from .http import adopt_manual_file, polite_get
from .results import (SOURCE_MODEL, SOURCE_SAMPLING_REPLICATE, ModelResult, check_refit_count,
                      check_replicate_policy, replicate_completeness)
from .variance import REPLICATE_COUNT, REPLICATE_FACTOR, Z90

USER_AGENT = "census-research-engine/pums (research toolkit; python-requests)"
PUMS_DATA_BASE = "https://www2.census.gov/programs-surveys/acs/data/pums"
PUMS_DOCS_BASE = "https://www2.census.gov/programs-surveys/acs/tech_docs/pums"
PUMS_API_TEMPLATE = "https://api.census.gov/data/{year}/acs/{dataset}/pums"
# Directory names under PUMS_DATA_BASE/<year>/ for each canonical product
# (census.api.PRODUCTS); product names are normalized by census.api.normalize_product.
PUMS_PRODUCT_DIRS = {PRODUCT_1YR: "1-Year", PRODUCT_5YR: "5-Year"}
RECORDS = {"person": "p", "housing": "h"}
WEIGHTS = {"person": "PWGTP", "housing": "WGTP"}
FIRST_DICTIONARY_CSV = {"1-year": 2017, "5-year": 2017}
MIN_UNWEIGHTED_N = 30
MAX_CV_PERCENT = 30.0
STATISTICS = ("count", "mean", "proportion", "median", "ratio")
METHOD_PUMS_REPLICATE = "pums_replicate_weights"
# Identifier and geography columns kept as zero-padded strings. Other columns
# become numeric when every value parses (so NAICSP/SOCP with letters stay text).
STRING_COLUMNS = {
    "RT", "SERIALNO", "DIVISION", "REGION", "STATE", "ST", "PUMA", "PUMA00", "PUMA10", "PUMA20",
    "MIGPUMA", "MIGPUMA10", "MIGPUMA20", "MIGSP", "POWPUMA", "POWPUMA10", "POWPUMA20", "POWSP",
    "NAICSP", "SOCP",
}
# Release notes for the 2020 1-year experimental PUMS (decision 3).
EXPERIMENTAL_2020_NOTE = (
    "2020 ACS 1-year PUMS are experimental (pandemic data collection; "
    f"{PUMS_DOCS_BASE}/A_Note_on_2020_ACS_1yr_PUMS.pdf)"
)

ID_COLUMNS = {"person": ["SERIALNO", "SPORDER", "STATE", "PUMA"], "housing": ["SERIALNO", "STATE", "PUMA"]}

STATE_POSTAL: Dict[str, str] = {f[2:4]: f[5:7] for f in STATE_FILES}


class PumsError(RuntimeError):
    """A PUMS file could not be resolved, downloaded or read."""


# --------------------------------------------------------------------------- names and URLs

def replicate_weight_columns(weight: str = "person") -> List[str]:
    base = weight_column(weight)
    return [f"{base}{i}" for i in range(1, REPLICATE_COUNT + 1)]


def weight_column(weight: str = "person") -> str:
    """'person' -> PWGTP, 'housing' -> WGTP; other names are returned upper-cased."""
    return WEIGHTS.get(str(weight).lower(), str(weight).upper())


def _product(product: str) -> str:
    return normalize_product(product)


def _record(record: str) -> str:
    r = str(record).lower()
    r = {"p": "person", "h": "housing", "persons": "person", "households": "housing"}.get(r, r)
    if r not in RECORDS:
        raise ValueError(f"record must be one of {sorted(RECORDS)}")
    return r


def state_fips(state: Union[str, int]) -> str:
    """FIPS code from a FIPS code, postal abbreviation or state name."""
    s = str(state).strip()
    if s.isdigit():
        return s.zfill(2)
    if len(s) == 2:
        inv = {v.lower(): k for k, v in STATE_POSTAL.items()}
        if s.lower() in inv:
            return inv[s.lower()]
    by_name = {v.lower(): k for k, v in STATE_MAPPING.items()}
    if s.lower() in by_name:
        return by_name[s.lower()]
    raise ValueError(f"unknown state {state!r}")


def _check_release(year: int, product: str, allow_experimental: bool) -> None:
    if product == "1-year" and year == 2020 and not allow_experimental:
        raise ValueError(f"{EXPERIMENTAL_2020_NOTE}; pass allow_experimental=True to use them")


def pums_url(state: Union[str, int], year: int, product: str = "5-year", record: str = "person") -> str:
    """URL of a state PUMS CSV zip."""
    st = state_fips(state)
    postal = STATE_POSTAL.get(st)
    if postal is None:
        raise ValueError(f"no PUMS state file code for FIPS {st}")
    return (f"{PUMS_DATA_BASE}/{int(year)}/{PUMS_PRODUCT_DIRS[_product(product)]}/"
            f"csv_{RECORDS[_record(record)]}{postal}.zip")


def data_dictionary_url(year: int, product: str = "5-year") -> str:
    p, year = _product(product), int(year)
    if year < FIRST_DICTIONARY_CSV[p]:
        raise ValueError(f"CSV data dictionaries start with the {FIRST_DICTIONARY_CSV[p]} {p} release "
                         f"(earlier releases publish .txt/.pdf under {PUMS_DOCS_BASE}/data_dict/)")
    years = str(year) if p == "1-year" else f"{year - 4}-{year}"
    return f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_{years}.csv"


def release_label(year: int, product: str) -> str:
    p = _product(product)
    return f"ACS {year} 1-year PUMS" if p == "1-year" else f"ACS {int(year) - 4}-{year} 5-year PUMS"


# --------------------------------------------------------------------------- cache

def pums_cache_dir() -> str:
    """$CENSUS_PUMS_DIR or <repo>/.cache/pums."""
    return os.getenv("CENSUS_PUMS_DIR") or get_resource_path(os.path.join(".cache", "pums"))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sidecar(path: str) -> str:
    return path + ".json"


def _stat_key(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    return int(st.st_size), int(st.st_mtime_ns)


def _write_sidecar(path: str, meta: Dict[str, Any]) -> None:
    with open(_sidecar(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def read_cache_entry(path: str, url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Sidecar metadata if ``path`` exists and matches its recorded SHA-256.

    With ``url``, a file placed at ``path`` by hand (no sidecar yet) is
    adopted with ``source: "manual download"`` (census.http.adopt_manual_file).

    State PUMS zips run to hundreds of MB, so the full SHA-256 is recomputed
    only when the file's size or mtime differs from the values recorded in
    the sidecar (``size_bytes``, ``mtime_ns``); a verified hash refreshes them.
    """
    if url is not None:
        adopt_manual_file(path, url, _sidecar(path))
    if not (os.path.exists(path) and os.path.exists(_sidecar(path))):
        return None
    try:
        with open(_sidecar(path), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    size, mtime = _stat_key(path)
    if meta.get("size_bytes") == size and meta.get("mtime_ns") == mtime and meta.get("sha256"):
        return meta
    if meta.get("sha256") != _sha256_file(path):
        return None
    meta["size_bytes"], meta["mtime_ns"] = size, mtime
    try:
        _write_sidecar(path, meta)
    except OSError:
        pass
    return meta


def write_cache_entry(path: str, raw: bytes, url: str, content_type: str = "") -> Dict[str, Any]:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)
    meta = {"source_url": url, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
            "content_type": content_type,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    meta["mtime_ns"] = _stat_key(path)[1]
    _write_sidecar(path, meta)
    return meta


def _download(url: str, session: Any = None, timeout: float = 600.0,
              cache_path: Optional[str] = None) -> Tuple[bytes, str]:
    resp = polite_get(url, user_agent=USER_AGENT, error=PumsError, session=session, timeout=timeout,
                      cache_path=cache_path)
    raw = resp.content or b""
    if resp.status_code == 404:
        raise PumsError(f"Not published (HTTP 404): {url}")
    if resp.status_code != 200:
        raise PumsError(f"HTTP {resp.status_code} for {url}")
    return raw, str(resp.headers.get("Content-Type", ""))


def cached_download(url: str, subdir: str = "", *, cache_dir: Optional[str] = None, refresh: bool = False,
                    session: Any = None) -> Tuple[str, Dict[str, Any]]:
    """Path and sidecar metadata of ``url`` in the PUMS cache, downloading if needed."""
    path = os.path.join(cache_dir or pums_cache_dir(), subdir, url.rsplit("/", 1)[-1])
    meta = None if refresh else read_cache_entry(path, url)
    if meta is None:
        raw, ctype = _download(url, session=session, cache_path=path)
        meta = write_cache_entry(path, raw, url, ctype)
    return path, meta


# --------------------------------------------------------------------------- reading

def _csv_member(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise PumsError(f"no CSV in PUMS archive: {zf.namelist()}")
    return names[0]


def _convert(frame: pd.DataFrame, keep_str: Iterable[str]) -> pd.DataFrame:
    keep = set(keep_str)
    for c in frame.columns:
        # pandas 2 reads text as object; pandas 3 as its "str" string dtype.
        is_text = pd.api.types.is_object_dtype(frame[c]) or pd.api.types.is_string_dtype(frame[c])
        if c in keep or not is_text:
            continue
        num = pd.to_numeric(frame[c], errors="coerce")
        if num.notna().sum() == frame[c].notna().sum():
            frame[c] = num
    return frame


# PLAN_ROUND_11 R2: reading every column of a large state file can exhaust
# memory; above this file size columns=None warns and suggests columns=.
LARGE_PUMS_BYTES = 200 * 1024 ** 2


class PumsMemoryWarning(UserWarning):
    """columns=None on a large PUMS file (PLAN_ROUND_11 R2)."""


# Shown on every call (not once per location): each call reads a file anew.
# Appended, so a user's own filters (e.g. "ignore" or "error") still take precedence.
warnings.filterwarnings("always", category=PumsMemoryWarning, append=True)


def _all_columns_note(source: Any) -> Optional[str]:
    """Warn (PumsMemoryWarning, a UserWarning) and return a note when all columns of a large file are requested."""
    try:
        if isinstance(source, str):
            size = os.path.getsize(source)
        elif isinstance(source, (bytes, bytearray)):
            size = len(source)
        else:
            pos = source.tell()
            size = source.seek(0, 2)
            source.seek(pos)
    except (OSError, AttributeError, ValueError):
        return None
    if size <= LARGE_PUMS_BYTES:
        return None
    name = os.path.basename(source) if isinstance(source, str) else getattr(source, "name", "in-memory data")
    note = (f"columns=None reads all 280+ columns of {name} ({size / 1024 ** 2:.0f} MB PUMS file); memory can run to "
            "tens of GB (hundreds for several states). Pass columns=[...] with only the variables you need "
            "(identifiers, PUMA and weights are always kept)")
    warnings.warn(note, PumsMemoryWarning, stacklevel=3)   # -> the caller of read_pums_csv
    return note


def read_pums_csv(source: Union[str, bytes, io.IOBase], record: str = "person",
                  columns: Optional[Sequence[str]] = None, *, replicates: bool = True,
                  chunksize: int = 200_000, filter: Optional[Callable[[pd.DataFrame], Any]] = None) -> pd.DataFrame:
    """Read a PUMS CSV (or a zip holding one), keeping only needed columns.

    Kept: ``columns`` (all if None), the identifiers in ``ID_COLUMNS``, every
    ``PUMA*`` column, the record's full weight and (``replicates=True``) its
    80 replicate weights. Column names are upper-cased (older files use
    lower-case replicate names). Weights are read as float32 (exact for the
    integer weights; sums are taken in float64). Code columns in
    ``STRING_COLUMNS`` stay strings; other all-numeric columns become numbers.
    ``filter(chunk) -> boolean mask`` drops rows while reading (memory); chunk
    values are still raw strings (weights float32), e.g. ``lambda c: c["PUMA"] == "00100"``.
    ``columns=None`` on a file larger than ``LARGE_PUMS_BYTES`` (200 MB) emits
    a UserWarning and sets ``attrs["memory_note"]`` (PLAN_ROUND_11 R2).
    """
    rec = _record(record)
    memory_note = _all_columns_note(source) if columns is None else None
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    handle, zf = source, None
    if isinstance(source, str) and source.lower().endswith(".zip") or (
            not isinstance(source, str) and zipfile.is_zipfile(source)):
        if not isinstance(source, str):
            source.seek(0)
        zf = zipfile.ZipFile(source)
        handle = zf.open(_csv_member(zf))
    elif not isinstance(source, str):
        source.seek(0)
    try:
        header = pd.read_csv(handle, nrows=0, compression="infer" if isinstance(handle, str) else None).columns
        if zf is not None:
            handle.close()
            handle = zf.open(_csv_member(zf))
        elif not isinstance(handle, str):
            handle.seek(0)
        upper = {str(c).upper(): c for c in header}
        base = WEIGHTS[rec]
        weights = [base] + (replicate_weight_columns(rec) if replicates else [])
        wanted = list(dict.fromkeys(
            [c for c in ID_COLUMNS[rec] if c in upper]
            + [c for c in upper if re.fullmatch(r"PUMA\d*", c)]
            + ([str(c).upper() for c in columns] if columns is not None else [c for c in upper])
            + weights))
        missing = [c for c in wanted if c not in upper]
        if missing:
            raise KeyError(f"columns not in this PUMS file: {missing} (variable codes change across releases; "
                           "check data_dictionary())")
        dtype = {upper[c]: ("float32" if c in weights else str) for c in wanted}
        reader = pd.read_csv(handle, usecols=[upper[c] for c in wanted], dtype=dtype, chunksize=chunksize,
                             keep_default_na=False, na_values=[""],
                             compression="infer" if isinstance(handle, str) else None)
        parts = []
        for chunk in reader:
            chunk.columns = [str(c).upper() for c in chunk.columns]
            if filter is not None:
                chunk = chunk.loc[np.asarray(filter(chunk), dtype=bool)]
            parts.append(chunk)
    finally:
        if zf is not None:
            handle.close()
            zf.close()
    frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=wanted)
    del parts  # the chunks, before conversion and column reordering make further copies
    frame = _convert(frame, STRING_COLUMNS | set(weights))
    frame = frame[[c for c in wanted if c in frame.columns]]
    if memory_note:
        frame.attrs["memory_note"] = memory_note
    return frame


def fetch_pums(
    state: Union[str, int],
    year: int,
    product: str = "5-year",
    record: str = "person",
    columns: Optional[Sequence[str]] = None,
    *,
    replicates: bool = True,
    filter: Optional[Callable[[pd.DataFrame], Any]] = None,
    allow_experimental: bool = False,
    cache_dir: Optional[str] = None,
    refresh: bool = False,
    session: Any = None,
    chunksize: int = 200_000,
) -> pd.DataFrame:
    """One state's PUMS person or housing records for a release.

    ``year`` is the release (end) year; ``product`` '5-year' or '1-year'.
    Pass ``columns`` to keep memory down (the 5-year California person file
    has about 1.9 million records and 280+ columns); identifiers, PUMA and the
    weights are always kept. The 2020 1-year file is experimental and needs
    ``allow_experimental=True`` (decision 3). ``attrs`` carry the release,
    source URL, SHA-256, cache path, PUMA vintage and weight names.

    Memory (PLAN_ROUND_11 R2, synthetic measurement 2026-11-07): a
    California-sized 5-year person file (1.9 million records) read with only
    the needed columns plus the 80 replicate weights peaked at about 3.6 GB
    RSS for a median by PUMA plus an 80-replicate regression. Reading all
    columns (``columns=None``) multiplies that several-fold, and multi-state
    all-column reads may need tens to hundreds of GB; ``columns=None`` on a
    zip larger than 200 MB therefore warns (UserWarning, attrs["memory_note"]).
    docs/audits/pums_memory.md (scripts/pums_memory_benchmark.py, Round 14)
    measures read, weight matrix and tabulation peaks at 100k-1.9M rows:
    about 2.8 KB per record with the needed columns (5.3 GiB at 1.9M rows).
    """
    p, rec, year = _product(product), _record(record), int(year)
    _check_release(year, p, allow_experimental)
    url = pums_url(state, year, p, rec)
    path, meta = cached_download(url, os.path.join(str(year), PUMS_PRODUCT_DIRS[p]), cache_dir=cache_dir,
                                 refresh=refresh, session=session)
    frame = read_pums_csv(path, rec, columns, replicates=replicates, chunksize=chunksize, filter=filter)
    try:
        vintage: Any = puma_vintage(year, p)
    except ValueError as exc:
        vintage = str(exc)
    frame.attrs.update(source="ACS PUMS", release=release_label(year, p), year=year, product=p, record=rec,
                       state=state_fips(state), source_url=url, sha256=meta.get("sha256"), cache_path=path,
                       weight=WEIGHTS[rec], replicate_weights=replicates, puma_vintage=vintage)
    if p == "1-year" and year == 2020:
        frame.attrs["advisory"] = EXPERIMENTAL_2020_NOTE
    return frame


# --------------------------------------------------------------------------- data dictionary

@dataclass
class PumsDictionary:
    """Variables and value labels of one PUMS release."""

    release: str
    source_url: str
    variables: pd.DataFrame  # name, type, length, description
    values: pd.DataFrame  # name, type, length, low, high, label

    def describe(self, name: str) -> str:
        row = self.variables[self.variables["name"] == name.upper()]
        if row.empty:
            raise KeyError(f"{name} not in the {self.release} data dictionary")
        return str(row["description"].iloc[0])

    def labels(self, name: str) -> Dict[str, str]:
        """Single-code labels {code: label} (ranges are omitted; see ``values``)."""
        v = self.values[(self.values["name"] == name.upper()) & (self.values["low"] == self.values["high"])]
        return dict(zip(v["low"], v["label"]))

    def label(self, series: pd.Series, name: Optional[str] = None) -> pd.Series:
        """Map codes in ``series`` to labels (numeric codes match ignoring zero padding;
        values inside a published range get that range's label; blanks get the 'b' label)."""
        name = (name or series.name or "").upper()
        v = self.values[self.values["name"] == name]
        if v.empty:
            raise KeyError(f"{name} has no value labels in the {self.release} data dictionary")
        exact: Dict[Any, str] = {}
        ranges: List[Tuple[float, float, str]] = []
        blank = None
        for lo, hi, lab in zip(v["low"], v["high"], v["label"]):
            if lo == "b":
                blank = lab
            elif lo == hi:
                exact[_code_key(lo)] = lab
            else:
                try:
                    ranges.append((float(lo), float(hi), lab))
                except ValueError:
                    pass

        def one(x: Any) -> Any:
            if x is None or (isinstance(x, float) and math.isnan(x)):
                return blank
            k = _code_key(x)
            if k in exact:
                return exact[k]
            if isinstance(k, (int, float)):
                for lo, hi, lab in ranges:
                    if lo <= k <= hi:
                        return lab
            return None

        return series.map(one)


def _code_key(x: Any) -> Any:
    s = str(x).strip()
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def parse_data_dictionary(raw: Union[bytes, str], release: str = "", source_url: str = "") -> PumsDictionary:
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    names, vals = [], []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        kind = row[0].strip().upper()
        if kind == "NAME" and len(row) >= 5:
            names.append({"name": row[1].strip().upper(), "type": row[2].strip(), "length": row[3].strip(),
                          "description": row[4].strip()})
        elif kind == "VAL" and len(row) >= 7:
            vals.append({"name": row[1].strip().upper(), "type": row[2].strip(), "length": row[3].strip(),
                         "low": row[4].strip(), "high": row[5].strip(), "label": row[6].strip()})
    variables = pd.DataFrame(names, columns=["name", "type", "length", "description"]).drop_duplicates("name")
    values = pd.DataFrame(vals, columns=["name", "type", "length", "low", "high", "label"]).drop_duplicates(
        ["name", "low", "high"])
    return PumsDictionary(release, source_url, variables.reset_index(drop=True), values.reset_index(drop=True))


def data_dictionary(year: int, product: str = "5-year", *, cache_dir: Optional[str] = None,
                    refresh: bool = False, session: Any = None) -> PumsDictionary:
    """The release's PUMS data dictionary (cached with a SHA-256 sidecar)."""
    p = _product(product)
    url = data_dictionary_url(year, p)
    path, _ = cached_download(url, "data_dict", cache_dir=cache_dir, refresh=refresh, session=session)
    with open(path, "rb") as f:
        return parse_data_dictionary(f.read(), release_label(int(year), p), url)


# --------------------------------------------------------------------------- tabulation core

def _weight_matrix(df: pd.DataFrame, weight: str) -> Tuple[str, np.ndarray]:
    base = weight_column(weight)
    cols = [base] + [f"{base}{i}" for i in range(1, REPLICATE_COUNT + 1)]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"replicate weights missing ({len(missing)} of 81 columns, e.g. {missing[:3]}); "
                       "read the file with replicates=True")
    return base, df[cols].to_numpy(np.float64)


def _mask(df: pd.DataFrame, where: Any) -> np.ndarray:
    if where is None:
        return np.ones(len(df), dtype=bool)
    if callable(where):
        out = where(df)
    elif isinstance(where, str):
        out = df.eval(where)
    else:
        out = where
    return np.asarray(pd.Series(np.asarray(out)).fillna(False), dtype=bool)


def _values(df: pd.DataFrame, expr: Any) -> np.ndarray:
    """A column, an expression evaluated with DataFrame.eval, or a callable."""
    if callable(expr):
        out = expr(df)
    elif isinstance(expr, str) and expr in df.columns:
        out = df[expr]
    elif isinstance(expr, str):
        out = df.eval(expr)
    else:
        out = expr
    return pd.to_numeric(pd.Series(np.asarray(out)), errors="coerce").to_numpy(np.float64)


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _defined(df: pd.DataFrame, expr: Any, x: np.ndarray) -> np.ndarray:
    """Rows where ``x`` is finite and, for a string expression, every column it
    references is non-missing (``df.eval("WAGP > 50000")`` yields False, not
    NaN, for a blank WAGP, which would otherwise count as a defined 0)."""
    ok = np.isfinite(x)
    if isinstance(expr, str) and expr not in df.columns:
        for name in dict.fromkeys(_IDENT.findall(expr)):
            if name in df.columns:
                ok &= df[name].notna().to_numpy()
    return ok


def _groups(df: pd.DataFrame, by: Optional[Union[str, Sequence[str]]]) -> Tuple[np.ndarray, pd.DataFrame]:
    if by is None:
        return np.zeros(len(df), dtype=np.int64), pd.DataFrame(index=[0])
    cols = [by] if isinstance(by, str) else list(by)
    g = df.groupby(cols, sort=True, dropna=True, observed=True)
    # Rows with a missing key get NaN from ngroup (float dtype); map them to -1
    # (dropped by _group_sum/_group_count) and keep integer group codes.
    ids = g.ngroup().fillna(-1).to_numpy().astype(np.int64)
    keys = g.size().reset_index()[cols]
    return ids, keys


def _group_sum(ids: np.ndarray, mat: np.ndarray, n_groups: int) -> np.ndarray:
    ok = ids >= 0
    out = np.zeros((n_groups, mat.shape[1]))
    if ok.any():
        # No copy of the n x 81 matrix when every row counts, and copy=False: pandas 3
        # copies a 2-D array passed to DataFrame by default (PLAN_ROUND_14 C).
        rows = ok.all()
        sums = pd.DataFrame(mat if rows else mat[ok], copy=False).groupby(ids if rows else ids[ok]).sum()
        out[sums.index.to_numpy()] = sums.to_numpy()
    return out


def _group_count(ids: np.ndarray, n_groups: int) -> np.ndarray:
    ok = ids >= 0
    return np.bincount(ids[ok], minlength=n_groups)


def _safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b != 0, a / np.where(b == 0, 1, b), np.nan)


def weighted_median(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted sample median for each weight column (``weights`` n x k).

    The smallest value whose cumulative weight reaches half the total; when
    the cumulative weight equals the half exactly, the midpoint of that value
    and the next. Returns k medians (NaN for a non-positive total).
    """
    x = np.asarray(values, float)
    w = np.asarray(weights, float)
    if w.ndim == 1:
        w = w[:, None]
    keep = np.flatnonzero(np.isfinite(x))
    if not len(keep):
        return np.full(w.shape[1], np.nan)
    # One gather of the weight rows (finite values in sorted order) rather than
    # two successive copies (PLAN_ROUND_14 C); the order is the same.
    keep = keep[np.argsort(x[keep], kind="mergesort")]
    x, w = x[keep], w[keep]
    cum = np.cumsum(w, axis=0)
    half = cum[-1] / 2.0
    reached = cum >= half - 1e-9 * np.abs(half)
    idx = reached.argmax(axis=0)
    out = x[idx].astype(float)
    exact = np.isclose(cum[idx, np.arange(w.shape[1])], half, rtol=1e-12, atol=1e-9)
    # At an exact half the midpoint uses the next record that carries weight:
    # zero-weight records are not part of the weighted distribution
    # (PLAN_ROUND_11 C2/D2). Negative replicate weights are not reinterpreted.
    for r in np.flatnonzero(exact):
        nz = np.flatnonzero(w[:, r] != 0)
        pos = np.searchsorted(nz, idx[r], side="right")
        if pos < len(nz):
            out[r] = (x[idx[r]] + x[nz[pos]]) / 2.0
    return np.where(half > 0, out, np.nan)


def binned_median(values: np.ndarray, weights: np.ndarray, bins: Sequence[float]) -> np.ndarray:
    """Census PUMS Accuracy of the Data median: weighted counts in brackets
    [bins[i], bins[i+1]) linearly interpolated (census.medians), per weight column."""
    from .medians import median_from_distribution

    edges = np.asarray(bins, float)
    x = np.asarray(values, float)
    w = np.asarray(weights, float)
    if w.ndim == 1:
        w = w[:, None]
    ok = np.isfinite(x)
    x, w = x[ok], w[ok]
    if len(x) and (x.min() < edges[0] or x.max() >= edges[-1]):
        raise ValueError(f"values outside the bins [{edges[0]}, {edges[-1]}); use -inf/inf end edges or wider bins")
    which = np.searchsorted(edges, x, side="right") - 1
    counts = np.zeros((len(edges) - 1, w.shape[1]))
    np.add.at(counts, which, w)
    return np.array([median_from_distribution(edges[:-1], edges[1:], counts[:, r]).estimate
                     for r in range(w.shape[1])])


def _finish(keys: pd.DataFrame, est: np.ndarray, n: np.ndarray, statistic: str, variable: str,
            base: str, notes: List[str], return_replicates: bool, min_n: int, max_cv: float,
            z: float = Z90) -> pd.DataFrame:
    full, reps = est[:, 0], est[:, 1:]
    # Any undefined replicate estimate (e.g. a zero replicate denominator)
    # makes the SE undefined: never drop it, rescale 4/80 or treat it as zero
    # sampling error (PLAN_ROUND_11 C1/D1).
    with np.errstate(invalid="ignore"):
        se = np.sqrt(REPLICATE_FACTOR * np.sum((reps - full[:, None]) ** 2, axis=1))
    se = np.where(np.isfinite(full), se, np.nan)
    se_undefined = np.isfinite(full) & ~np.isfinite(se)
    if se_undefined.any():
        notes = list(notes) + [f"{int(se_undefined.sum())} estimate(s) have undefined replicate estimates "
                               "(e.g. a zero replicate denominator): se, moe and cv are NaN "
                               "(flag_se_undefined), not zero"]
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where(full != 0, se / np.abs(full) * 100.0, np.nan)
    out = keys.reset_index(drop=True).copy()
    out["statistic"] = statistic
    out["variable"] = variable
    out["estimate"] = full
    out["se"] = se
    out["moe"] = z * se
    out["cv_percent"] = cv
    out["n_unweighted"] = n.astype(int)
    out["flag_small_n"] = out["n_unweighted"] < min_n
    out["flag_high_cv"] = np.nan_to_num(cv, nan=0.0) > max_cv
    out["flag_se_undefined"] = se_undefined
    out["reliability_flags"] = [";".join(f for f, on in ((f"n<{min_n}", a), (f"cv>{max_cv:g}%", b),
                                                         ("se_undefined", c)) if on)
                                for a, b, c in zip(out["flag_small_n"], out["flag_high_cv"], se_undefined)]
    out["method"] = METHOD_PUMS_REPLICATE
    if return_replicates:
        rep = pd.DataFrame(reps, columns=[f"rep_{i}" for i in range(1, REPLICATE_COUNT + 1)])
        out = pd.concat([out, rep], axis=1)
    out.attrs.update(weight=base, variance=("Var = 4/80 * sum_r (X_r - X)^2 over the 80 replicate weights "
                                            f"{base}1-{base}80"), notes=notes,
                     small_cell_rule=f"flagged (not suppressed) when n_unweighted < {min_n} or CV > {max_cv:g}%")
    return out


def tabulate(
    df: pd.DataFrame,
    statistic: str = "count",
    var: Any = None,
    by: Optional[Union[str, Sequence[str]]] = None,
    where: Any = None,
    weight: str = "person",
    *,
    denominator: Any = None,
    bins: Optional[Sequence[float]] = None,
    return_replicates: bool = False,
    min_n: int = MIN_UNWEIGHTED_N,
    max_cv: float = MAX_CV_PERCENT,
) -> pd.DataFrame:
    """Weighted PUMS estimate with replicate-weight SE, MOE (90%), CV and unweighted n.

    * ``count``: sum of weights of records satisfying ``where`` (``var``, if
      given, is an extra condition, e.g. ``"AGEP >= 65"``).
    * ``mean``: weighted mean of ``var`` (records with ``var`` missing dropped).
    * ``proportion``: weighted share (0-1) of records with ``var`` true/nonzero
      among records with ``var`` defined; ``var`` may be a column, an
      expression such as ``"SCHL >= 21"``, or a callable. For an expression,
      records where any referenced column is missing are excluded from both
      numerator and denominator (and from ``n_unweighted``).
    * ``median``: weighted median of ``var`` (``bins`` for the Census
      bracket-interpolation method; see module docstring).
    * ``ratio``: sum(w * var) / sum(w * denominator), rows with both defined.

    ``where`` restricts the universe (expression, mask or callable); ``by``
    groups (rows with a missing group key are dropped). One row per group with
    columns: group keys, statistic, variable, estimate, se, moe, cv_percent,
    n_unweighted, flag_small_n, flag_high_cv, flag_se_undefined (finite estimate
    with undefined replicate SE), reliability_flags, method (and rep_1..rep_80
    with ``return_replicates``). Flags follow decision 2 (never suppressed).
    """
    statistic = statistic.lower()
    if statistic not in STATISTICS:
        raise ValueError(f"statistic must be one of {STATISTICS}")
    if statistic != "count" and var is None:
        raise ValueError(f"statistic {statistic!r} needs var")
    if statistic == "ratio" and denominator is None:
        raise ValueError("statistic 'ratio' needs denominator")
    keep = _mask(df, where)
    sub = df.loc[keep]
    base, W = _weight_matrix(sub, weight)
    ids, keys = _groups(sub, by)
    G = len(keys)
    notes: List[str] = []
    label = var if isinstance(var, str) else ("records" if var is None else getattr(var, "__name__", "var"))

    if statistic == "count":
        ok = np.ones(len(sub), bool) if var is None else np.nan_to_num(_values(sub, var)) != 0
        ids_ok = np.where(ok, ids, -1)
        est = _group_sum(ids_ok, W, G)
        n = _group_count(ids_ok, G)
    elif statistic in ("mean", "proportion"):
        x = _values(sub, var)
        defined = _defined(sub, var, x)
        if statistic == "proportion":
            x = (np.nan_to_num(x) != 0).astype(float)
            notes.append("proportion is a fraction (0-1) of records with the variable defined")
        ids_ok = np.where(defined, ids, -1)
        num = _group_sum(ids_ok, W * np.where(defined, x, 0.0)[:, None], G)
        den = _group_sum(ids_ok, W, G)
        est = _safe_div(num, den)
        n = _group_count(ids_ok, G)
    elif statistic == "ratio":
        x, y = _values(sub, var), _values(sub, denominator)
        defined = _defined(sub, var, x) & _defined(sub, denominator, y)
        ids_ok = np.where(defined, ids, -1)
        num = _group_sum(ids_ok, W * np.where(defined, x, 0.0)[:, None], G)
        den = _group_sum(ids_ok, W * np.where(defined, y, 0.0)[:, None], G)
        est = _safe_div(num, den)
        n = _group_count(ids_ok, G)
        label = f"{label} / {denominator if isinstance(denominator, str) else 'denominator'}"
    else:  # median
        x = _values(sub, var)
        defined = _defined(sub, var, x)
        ids_ok = np.where(defined, ids, -1)
        est = np.full((G, REPLICATE_COUNT + 1), np.nan)
        # One stable sort of the group ids; each group's rows are then a contiguous slice
        # in original row order, gathered once (not G arrays of length N). Same rows in
        # the same order as masking, so the medians are bit-identical.
        order = np.argsort(ids_ok, kind="stable")
        bounds = np.searchsorted(ids_ok[order], np.arange(G + 1), side="left")
        for g in range(G):
            rows = order[bounds[g]:bounds[g + 1]]
            if len(rows):
                xg, wg = x[rows], W[rows]
                est[g] = (binned_median(xg, wg, bins) if bins is not None else weighted_median(xg, wg))
        n = _group_count(ids_ok, G)
        notes.append("median by Census bracket interpolation over bins" if bins is not None
                     else "weighted sample median (no interpolation); pass bins for the Census bracket method")
    out = _finish(keys, est, n, statistic, str(label), base, notes, return_replicates, min_n, max_cv)
    out.attrs["provenance"] = {k: v for k, v in df.attrs.items() if isinstance(v, (str, int, float, bool))}
    return out


def crosstab(
    df: pd.DataFrame,
    row: str,
    col: str,
    weight: str = "person",
    where: Any = None,
    normalize: Optional[str] = None,
    *,
    return_replicates: bool = False,
    min_n: int = MIN_UNWEIGHTED_N,
    max_cv: float = MAX_CV_PERCENT,
) -> pd.DataFrame:
    """Weighted two-way table in long form (one row per row x col cell).

    ``normalize``: None (counts), 'row' (share of the row total), 'column',
    or 'all'. Shares are ratios of weighted sums, so their SEs come from the
    same replicate formula. Columns as in :func:`tabulate`.
    """
    if normalize not in (None, "row", "column", "all"):
        raise ValueError("normalize must be None, 'row', 'column' or 'all'")
    keep = _mask(df, where)
    sub = df.loc[keep]
    base, W = _weight_matrix(sub, weight)
    ids, keys = _groups(sub, [row, col])
    G = len(keys)
    cell = _group_sum(ids, W, G)
    n = _group_count(ids, G)
    statistic = "count"
    if normalize is not None:
        if normalize == "all":
            den = np.repeat(cell.sum(axis=0, keepdims=True), G, axis=0)
        else:
            k = row if normalize == "row" else col
            codes, _ = pd.factorize(keys[k])
            tot = _group_sum(codes, cell, int(codes.max()) + 1 if len(codes) else 0)
            den = tot[codes]
        cell = _safe_div(cell, den)
        statistic = f"proportion_{normalize}"
    out = _finish(keys, cell, n, statistic, f"{row} x {col}", base, [], return_replicates, min_n, max_cv)
    out.attrs["provenance"] = {k: v for k, v in df.attrs.items() if isinstance(v, (str, int, float, bool))}
    return out


# --------------------------------------------------------------------------- regression

PUMS_MODELS = ("ols", "logit", "poisson")


def _irls(X: np.ndarray, y: np.ndarray, w: np.ndarray, model: str, start: Optional[np.ndarray] = None,
          max_iter: int = 100, tol: float = 1e-10) -> np.ndarray:
    """Weighted ML by Newton/IRLS; weights may be negative (some replicate weights are)."""
    if model == "ols":
        A = X.T @ (X * w[:, None])
        return np.linalg.solve(A, X.T @ (w * y))
    b = np.zeros(X.shape[1]) if start is None else start.copy()
    for _ in range(max_iter):
        eta = np.clip(X @ b, -30, 30)
        if model == "logit":
            mu = 1.0 / (1.0 + np.exp(-eta))
            v = mu * (1 - mu)
        else:
            mu = np.exp(eta)
            v = mu
        grad = X.T @ (w * (y - mu))
        H = X.T @ (X * (w * v)[:, None])
        step = np.linalg.solve(H, grad)
        b = b + step
        if np.max(np.abs(step)) < tol * (1 + np.max(np.abs(b))):
            return b
    raise RuntimeError("IRLS did not converge")


def _sandwich(X: np.ndarray, y: np.ndarray, w: np.ndarray, b: np.ndarray, model: str) -> np.ndarray:
    eta = X @ b
    if model == "ols":
        mu, v = eta, np.ones_like(eta)
    elif model == "logit":
        mu = 1.0 / (1.0 + np.exp(-eta))
        v = mu * (1 - mu)
    else:
        mu = np.exp(eta)
        v = mu
    bread = np.linalg.inv(X.T @ (X * (w * v)[:, None]))
    s = X * (w * (y - mu))[:, None]
    n, k = X.shape
    meat = s.T @ s * (n / max(n - k, 1))
    return np.sqrt(np.diag(bread @ meat @ bread))


def pums_regression(
    formula: str,
    df: pd.DataFrame,
    weight: str = "person",
    model: str = "ols",
    where: Any = None,
    target: str = "these_areas",
    confidence_level: float = 0.90,
    replicate_policy: str = "flag",
    max_failed_replicates: int = 0,
) -> ModelResult:
    """Survey-weighted regression on PUMS with replicate-weight SEs.

    The model (``ols`` = weighted least squares, ``logit``, ``poisson``) is
    fit with the full weight and refit with each of the 80 replicate weights;
    ``se_sampling = sqrt(4/80 * sum_r (b_r - b)^2)`` (the PUMS replicate
    formula). ``se_model`` is the weighted sandwich (HC1) SE, which treats
    records as independent and ignores the PUMS design (household clustering,
    replicate structure); it is reported for comparison.

    Interval targets (census.models.INTERVAL_TARGETS): the default
    ``"these_areas"`` uses se_sampling, the design-based SE for the population
    the PUMS represents (Census's documented method for PUMS); ``"population"``
    uses se_model; ``"conservative"`` uses sqrt(se_model^2 + se_sampling^2).

    ``replicate_policy`` (census.results.replicate_completeness, PLAN_ROUND_11
    Q2/D7): a replicate refit fails when IRLS does not converge, the system is
    singular, or a replicate weight is missing (non-finite) for a modelled
    record. "flag" (default) omits up to ``max_failed_replicates`` (default
    0, as in census.models.fit_replicate) failed refits from se_sampling and
    records diagnostics["replicates_incomplete"]; more failures raise
    RuntimeError. "strict" raises IncompleteReplicatesError (a ValueError) on
    any failure. If every refit fails, RuntimeError (flag) or
    IncompleteReplicatesError (strict) is raised.
    """
    import patsy

    from .models import INTERVAL_TARGETS, _table

    model = model.lower()
    check_replicate_policy(replicate_policy)
    if model not in PUMS_MODELS:
        raise ValueError(f"model must be one of {PUMS_MODELS}")
    if target not in INTERVAL_TARGETS:
        raise ValueError(f"target must be one of {INTERVAL_TARGETS}")
    # Positional index: concatenated multi-state frames repeat row labels,
    # which would make the label lookup below ambiguous.
    sub = df.loc[_mask(df, where)].reset_index(drop=True)
    base, W = _weight_matrix(sub, weight)
    y_df, X_df = patsy.dmatrices(formula, sub, return_type="dataframe", NA_action="drop")
    pos = sub.index.get_indexer(X_df.index)
    W = W[pos]
    X, y = X_df.to_numpy(float), y_df.iloc[:, 0].to_numpy(float)
    if model == "logit" and not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("logit outcome must be 0/1")
    b0 = _irls(X, y, W[:, 0], model)
    coefs, failed = [], 0
    for r in range(1, REPLICATE_COUNT + 1):
        try:
            if not np.isfinite(W[:, r]).all():
                raise ValueError("non-finite replicate weight")
            b = _irls(X, y, W[:, r], model, start=b0)
            if not np.isfinite(b).all():
                raise ValueError("non-finite coefficients")
            coefs.append(b)
        except (np.linalg.LinAlgError, RuntimeError, ValueError):
            failed += 1
    notes: List[str] = []
    diagnostics: Dict[str, Any] = {}
    replicate_completeness(replicate_policy, failed_refits=failed, context="pums_regression",
                           diagnostics=diagnostics, notes=notes)
    check_refit_count(failed, len(coefs), max_failed_replicates, "pums_regression")
    B = np.vstack(coefs)
    se_samp = np.sqrt(REPLICATE_FACTOR * ((B - b0) ** 2).sum(axis=0))
    se_mod = _sandwich(X, y, W[:, 0], b0, model)
    terms = list(X_df.columns)
    table = _table(pd.Series(b0, index=terms), pd.Series(se_mod, index=terms),
                   pd.Series(se_samp, index=terms), confidence_level, target)
    notes = notes + [
        "se_sampling: refit with each of the 80 replicate weights, Var = 4/80 * sum_r (b_r - b)^2",
        "se_model: weighted sandwich (HC1) SE ignoring the PUMS design; for comparison only",
        f"intervals and p-values use the '{target}' target",
    ]
    dropped = len(sub) - len(X_df)
    if dropped:
        notes.append(f"{dropped} record(s) dropped for missing formula variables")
    diagnostics.update({
        "interval_target": target, "weight": base, "n_unweighted": int(len(X_df)),
        "weighted_n": float(W[:, 0].sum()),
        "replicate_stability": {t: {"sd": float(B[:, j].std(ddof=1)),
                                    "share_same_sign": float(np.mean(np.sign(B[:, j]) == np.sign(b0[j])))}
                                for j, t in enumerate(terms)},
    })
    provenance = {k: v for k, v in df.attrs.items() if isinstance(v, (str, int, float, bool))}
    provenance["model_library"] = "numpy IRLS (census.pums)"
    return ModelResult(model_type=f"pums_{model}", formula=formula, n_obs=int(len(X_df)), coefficients=table,
                       method="pums_replicate_weight_refit",
                       uncertainty_sources=[SOURCE_MODEL, SOURCE_SAMPLING_REPLICATE],
                       confidence_level=confidence_level, notes=notes, diagnostics=diagnostics,
                       provenance=provenance)


# --------------------------------------------------------------------------- PUMA geography

PUMA_TRACT_RELATIONSHIP_URLS = {
    2010: "https://www2.census.gov/geo/docs/maps-data/data/rel/2010_Census_Tract_to_2010_PUMA.txt",
    2020: "https://www2.census.gov/geo/docs/maps-data/data/rel2020/2020_Census_Tract_to_2020_PUMA.txt",
}


PUMS_DATA_DICTIONARY_PUMA_SOURCES = (
    # Verified 2026-11: PUMA columns in the official CSV data dictionaries.
    f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_2017-2021.csv",  # PUMA, 2010 definition
    f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_2018-2022.csv",  # PUMA10 + PUMA20, -0009 = N/A
    f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_2019-2023.csv",  # PUMA, 2020 definition
    f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_2020-2024.csv",  # PUMA, 2020 definition
    f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_2021.csv",       # 1-year: PUMA, 2010
    f"{PUMS_DOCS_BASE}/data_dict/PUMS_Data_Dictionary_2022.csv",       # 1-year: PUMA, 2020
)
PUMA_NOT_APPLICABLE = "-0009"


def puma_vintage(year: int, product: str = "5-year") -> int:
    """Census PUMA set (2000, 2010 or 2020) coded in a PUMS release.

    Per the official PUMS data dictionaries (``PUMS_DATA_DICTIONARY_PUMA_SOURCES``):

    * 1-year: 2010 PUMAs from 2012 through 2021, 2020 PUMAs from 2022.
    * 5-year: 2010 PUMAs for 2012-2016 through 2017-2021; one ``PUMA`` column
      on the 2020 definition for 2019-2023 and 2020-2024 (the 2019-2021 sample
      years are recoded to 2020 PUMAs).
    * Mixed releases raise ValueError: 2008-2012 through 2011-2015 carry
      PUMA00/PUMA10, and 2018-2022 carries PUMA10 (data years 2018-2021) and
      PUMA20 (data year 2022) with ``-0009`` (not applicable) in the other
      column, so no single PUMA vintage covers every record.
    """
    p, year = _product(product), int(year)
    if p == "1-year":
        if year >= 2022:
            return 2020
        return 2010 if year >= 2012 else 2000
    if year == 2022:
        raise ValueError("the 2018-2022 5-year PUMS mixes 2010 and 2020 PUMAs (PUMA10 for data years "
                         "2018-2021, PUMA20 for 2022; -0009 = not applicable): pick one column explicitly")
    if year >= 2023:
        return 2020
    if year >= 2016:
        return 2010
    if year <= 2011:
        return 2000
    raise ValueError(f"the {year - 4}-{year} 5-year PUMS mixes 2000 and 2010 PUMAs (PUMA00/PUMA10 columns)")


def puma_geoid(df: pd.DataFrame, puma: str = "PUMA", state: str = "STATE") -> pd.Series:
    """7-character PUMA GEOID (state FIPS + 5-digit PUMA), matching boundary files.

    Mixed-vintage files (2018-2022 5-year: PUMA10/PUMA20; 2008-2012 to 2011-2015:
    PUMA00/PUMA10) have no ``PUMA`` column: pass ``puma="PUMA20"`` (or another
    vintage column) explicitly. Records coded ``-0009`` (not applicable: the
    record's data year is outside that column's vintage) get a missing GEOID.
    """
    if puma not in df.columns:
        alts = [c for c in df.columns if re.fullmatch(r"PUMA\d\d", str(c))]
        if alts:
            raise ValueError(f"no {puma!r} column: this release mixes PUMA vintages ({', '.join(alts)}); "
                             "pass puma=<one of them>; records outside its vintage get a missing GEOID")
        raise KeyError(puma)
    st = df[state].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(2)
    raw = df[puma].astype(str).str.replace(r"\.0$", "", regex=True)
    pu = raw.str.zfill(5)
    na = df[puma].isna() | raw.str.startswith("-")
    return (st + pu).mask(na).rename("GEOID")


def add_puma_geoid(df: pd.DataFrame, puma: str = "PUMA", state: str = "STATE") -> pd.DataFrame:
    out = df.copy()
    out["GEOID"] = puma_geoid(df, puma, state)
    out.attrs.update(df.attrs)
    return out


def puma_boundaries(year: int, state: Optional[Union[str, int]] = None, product: str = "5-year",
                    detail: str = "cb", **kwargs: Any):
    """PUMA polygons matching a PUMS release (census.boundaries level 'puma')."""
    from .boundaries import boundary_vintage, get_boundaries

    _product(product)
    vintage = boundary_vintage(int(year), "puma", detail)
    return get_boundaries("puma", vintage, state_fips(state) if state is not None else None, detail, **kwargs)


def puma_tract_relationship(vintage: int, state: Optional[Union[str, int]] = None, *,
                            cache_dir: Optional[str] = None, refresh: bool = False,
                            session: Any = None) -> pd.DataFrame:
    """Census tract-to-PUMA relationship file (2010 or 2020) with tract and PUMA GEOIDs."""
    if int(vintage) not in PUMA_TRACT_RELATIONSHIP_URLS:
        raise ValueError(f"vintage must be one of {sorted(PUMA_TRACT_RELATIONSHIP_URLS)}")
    url = PUMA_TRACT_RELATIONSHIP_URLS[int(vintage)]
    path, meta = cached_download(url, "relationship", cache_dir=cache_dir, refresh=refresh, session=session)
    rel = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    rel.columns = [c.strip().upper() for c in rel.columns]
    pcol = next(c for c in rel.columns if c.startswith("PUMA"))
    rel["TRACT_GEOID"] = rel["STATEFP"] + rel["COUNTYFP"] + rel["TRACTCE"]
    rel["PUMA_GEOID"] = rel["STATEFP"] + rel[pcol]
    if state is not None:
        rel = rel[rel["STATEFP"] == state_fips(state)]
    rel = rel.reset_index(drop=True)
    rel.attrs.update(source_url=url, sha256=meta.get("sha256"), vintage=int(vintage))
    return rel
