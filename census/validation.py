# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validation of the toolkit's calculations against values Census publishes.

Every check recomputes a number the Census Bureau computed independently from
the same survey data, and compares (PLAN_ROUND_7.md section 8). Comparing our
API output with the API itself would be circular; the only exception is F1,
which checks that the pipeline does not lose, shift or mislabel cells.

======  ==================================================================
check   what is compared
======  ==================================================================
F1      run_batch tidy panel (CSV) and Parquet cells == raw API cells
F2      derived.proportion from detailed tables vs Data Profile percents
F3      replicate MOEs (VRE files) vs published MOEs; VRE cells == API cells
F4      exact replicate proportions vs Data Profile percents and MOEs
F5      medians from distribution tables vs published medians
F6      prior-period dollars adjusted with R-CPI-U-RS vs Comparison Profile
F7      stats.z_test vs Comparison Profile significance flags
F8      sum of counties (aggregate.aggregate_geographies) vs state totals
F9      Round 6 national continuity check (from the F1 extraction)
F10     table-based Summary File cells (www2.census.gov) == API cells
======  ==================================================================

Data come from a :class:`CensusSource`. Offline (the default) it reads frozen
responses from ``tests/fixtures/validation/`` (each file has a ``.json``
sidecar with its source URL, retrieval date and SHA-256); live it queries
api.census.gov and www2.census.gov, and with ``record=True`` it (re)writes
those fixtures. The API key is never written anywhere: URLs are recorded
without it and error messages are redacted.

Entry points: :func:`run_battery` (returns ``ValidationResult`` objects),
:func:`run_validation` (runs and writes ``validation_report.md``/``.json``),
:func:`write_markdown_report`, :func:`write_json_report`.
"""

import datetime as _dt
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from census import aggregate as _aggregate
from census import derived as _derived
from census import inflation as _inflation
from census import medians as _medians
from census import metadata as _md
from census import replicate as _replicate
from census.api import BASE_URL, DATASETS, CensusAPIClient
from census.stats import CENSUS_SENTINELS, acs5_overlap_years, parse_variable_code, z_test

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "validation")
DEFAULT_REPORT_DIR = os.path.join(REPO_ROOT, "docs", "audits")
REPORT_BASENAME = "validation_report"

CHECK_IDS: Tuple[str, ...] = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10")

# Census API: at most 50 variables per request (NAME counts).
MAX_GET_VARIABLES = 48
USER_AGENT = "acs-batch-downloader/validation (research toolkit; python-requests)"
WWW2_DELAY_SECONDS = 3.0  # census.gov firewall rejects rapid scripted requests

SUMMARY_FILE_URL = (
    "https://www2.census.gov/programs-surveys/acs/summary_file/{year}/table-based-SF/data/"
    "5YRData/acsdt5y{year}-{table}.dat"
)
SUMMARY_FILE_DOC = "https://www.census.gov/programs-surveys/acs/data/summary-file.html"
SUMMARY_FILE_CHUNK = 262_144
SUMMARY_FILE_MAX_BYTES = 4 * 1024 * 1024

HANDBOOK_CH8 = (
    "https://www.census.gov/content/dam/Census/library/publications/2020/acs/"
    "acs_general_handbook_2020_ch08.pdf"
)
SUBJECT_DEFINITIONS_2023 = (
    "https://www2.census.gov/programs-surveys/acs/tech_docs/subject_definitions/"
    "2023_ACSSubjectDefinitions.pdf"
)
CP_USER_NOTES = "https://www.census.gov/programs-surveys/acs/guidance/comparing-acs-data.html"
CPI_GUIDANCE = (
    "https://www.census.gov/topics/income-poverty/income/guidance/current-vs-constant-dollars.html"
)

_SENTINELS = {float(k) for k in CENSUS_SENTINELS}
CONTROLLED_MOE = -555555555.0


# ---------------------------------------------------------------------------
# Battery configuration
# ---------------------------------------------------------------------------


@dataclass
class Battery:
    """What the battery covers (PLAN_ROUND_7.md section 8, F3 "default battery")."""

    states: Tuple[str, ...] = ("06", "36", "48", "17", "56")
    releases: Tuple[int, ...] = (2019, 2023)
    # Comparison Profile release -> the earlier, non-overlapping release it compares.
    cp_releases: Tuple[Tuple[int, int], ...] = ((2019, 2014), (2023, 2018))
    # F1: one whole state (every county) and two counties of another state.
    f1_table: str = "B01001"
    # F9 also reports (not asserts) the continuity flags of these tables, which
    # the Round 6 check flags for real 2019->2023 change (false positives).
    f9_info_tables: Tuple[str, ...] = ("B19001", "B17001", "B27010")
    f1_state: str = "56"
    f1_extra_state: str = "06"
    f1_extra_counties: Tuple[str, ...] = ("001", "037")
    f3_tables: Tuple[str, ...] = (
        "B17001", "B27010", "B25003", "B01001", "B01003", "B11001", "B19001", "B25063",
    )
    f8_variables: Tuple[str, ...] = tuple(
        ["B01003_001", "B25001_001"] + [f"B11001_{i:03d}" for i in range(1, 10)]
    )
    # F4 also at county level for these constructions (national county VRE file ~2 MB).
    f4_county_constructions: Tuple[str, ...] = ("owner_occupied",)
    sf_release: int = 2023
    sf_tables: Tuple[str, ...] = ("B01003", "B25003", "B17001", "B19001")

    def state_list(self) -> str:
        return ",".join(self.states)


DEFAULT_BATTERY = Battery()


# Data Profile items rebuilt from detailed tables (F2, F4). Each pairing is
# verified against the release's labels before it is used (see
# _verify_construction); a label mismatch skips the pairing for that release
# as a validation-design problem, never as a toolkit failure.
@dataclass(frozen=True)
class Construction:
    id: str
    dp: str  # Data Profile base code (PE/PM are compared)
    dp_label: str  # expected label path after "Percent!!"
    table: str
    num: Tuple[str, ...]
    den: str
    num_labels: Tuple[str, ...]  # each numerator label ends with one of these
    simple: bool  # single-cell numerator: MOE tolerance is asserted (F2)
    vre: bool  # table has Variance Replicate Estimates (F4)
    definition: str


CONSTRUCTIONS: Tuple[Construction, ...] = (
    Construction(
        id="poverty_rate",
        dp="DP03_0128",
        dp_label=(
            "PERCENTAGE OF FAMILIES AND PEOPLE WHOSE INCOME IN THE PAST 12 MONTHS IS BELOW THE "
            "POVERTY LEVEL!!All people"
        ),
        table="B17001",
        num=("B17001_002",),
        den="B17001_001",
        num_labels=("Income in the past 12 months below poverty level",),
        simple=True,
        vre=True,
        definition=(
            "Percent of people whose income in the past 12 months is below the poverty level. "
            "Universe of both: population for whom poverty status is determined (Subject "
            "Definitions, 'Poverty Status'). DP03 'All people' = B17001_002 / B17001_001."
        ),
    ),
    Construction(
        id="uninsured_rate",
        dp="DP03_0099",
        dp_label="HEALTH INSURANCE COVERAGE!!Civilian noninstitutionalized population!!No health insurance coverage",
        table="B27010",
        num=("B27010_017", "B27010_033", "B27010_050", "B27010_066"),
        den="B27010_001",
        num_labels=("No health insurance coverage",),
        simple=False,
        vre=True,
        definition=(
            "Percent of the civilian noninstitutionalized population with no health insurance "
            "coverage. B27010 (types of health insurance coverage by age; universe civilian "
            "noninstitutionalized population): the four age groups' 'No health insurance "
            "coverage' cells (under 19, 19-34, 35-64, 65+) over B27010_001."
        ),
    ),
    Construction(
        id="owner_occupied",
        dp="DP04_0046",
        dp_label="HOUSING TENURE!!Occupied housing units!!Owner-occupied",
        table="B25003",
        num=("B25003_002",),
        den="B25003_001",
        num_labels=("Owner occupied",),
        simple=True,
        vre=True,
        definition=(
            "Percent of occupied housing units that are owner-occupied. B25003 (tenure; universe "
            "occupied housing units): B25003_002 / B25003_001."
        ),
    ),
    Construction(
        id="age_65_plus",
        dp="DP05_0024",
        dp_label="SEX AND AGE!!Total population!!65 years and over",
        table="B01001",
        num=tuple(f"B01001_{i:03d}" for i in list(range(20, 26)) + list(range(44, 50))),
        den="B01001_001",
        num_labels=(
            "65 and 66 years", "67 to 69 years", "70 to 74 years", "75 to 79 years",
            "80 to 84 years", "85 years and over",
        ),
        simple=False,
        vre=True,
        definition=(
            "Percent of the total population aged 65 and over. B01001 (sex by age; universe "
            "total population): male and female 65+ cells (_020-_025, _044-_049, 12 cells) over "
            "B01001_001. B01001_001 is a controlled estimate (published MOE '*****')."
        ),
    ),
    Construction(
        id="bachelors_or_higher",
        dp="DP02_0068",
        dp_label="EDUCATIONAL ATTAINMENT!!Population 25 years and over!!Bachelor's degree or higher",
        table="B15003",
        num=("B15003_022", "B15003_023", "B15003_024", "B15003_025"),
        den="B15003_001",
        num_labels=(
            "Bachelor's degree", "Master's degree", "Professional school degree", "Doctorate degree",
        ),
        simple=False,
        vre=False,
        definition=(
            "Percent of the population 25 years and over with a bachelor's degree or higher. "
            "B15003 (educational attainment; universe population 25 years and over): bachelor's, "
            "master's, professional and doctorate cells over B15003_001. B15003 has no VRE file."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


@dataclass
class ValidationResult:
    """Outcome of one validation check (PLAN_ROUND_7.md section 8, F2)."""

    check_id: str
    description: str
    n_compared: int = 0
    n_failed: int = 0
    max_abs_error: float = math.nan
    max_rel_error: float = math.nan
    tolerance: str = ""
    failures: List[Dict[str, Any]] = field(default_factory=list)
    census_source: List[str] = field(default_factory=list)
    passed: Optional[bool] = None  # None when skipped
    notes: List[str] = field(default_factory=list)
    status: str = STATUS_PASSED
    details: Dict[str, Any] = field(default_factory=dict)
    mode: str = ""
    seconds: float = 0.0

    def finish(self, passed: bool) -> "ValidationResult":
        self.passed = bool(passed)
        self.status = STATUS_PASSED if passed else STATUS_FAILED
        return self

    def skip(self, reason: str) -> "ValidationResult":
        self.passed = None
        self.status = STATUS_SKIPPED
        self.notes.append(reason)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return _json_safe(asdict(self))


MAX_FAILURE_SAMPLES = 25
INCOME_MEDIAN_MIN_SHARE_IN_MOE = 0.98


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        v = float(value)
        return v if math.isfinite(v) else None
    return value


def _max(values: Iterable[float]) -> float:
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(arr.max()) if arr.size else math.nan


def _distribution(values: Iterable[float]) -> Dict[str, Any]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    if not arr.size:
        return {"n": 0}
    q = np.percentile(arr, [5, 25, 50, 75, 95])
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p5": float(q[0]),
        "p25": float(q[1]),
        "median": float(q[2]),
        "p75": float(q[3]),
        "p95": float(q[4]),
        "max": float(arr.max()),
    }


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _numeric(values: Any) -> np.ndarray:
    """Float array with Census sentinel codes (and non-numbers) as NaN."""
    s = pd.to_numeric(pd.Series(values), errors="coerce").astype(float)
    return s.mask(s.isin(_SENTINELS)).to_numpy()


def _raw_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _norm_label(label: str) -> str:
    """Label path without its prefix segment, colons, case, or dollar year."""
    parts = [p.strip().rstrip(":").strip() for p in str(label).split("!!")]
    s = "!!".join(parts[1:] if len(parts) > 1 else parts).upper()
    s = re.sub(r"IN \d{4} INFLATION-ADJUSTED DOLLARS", "IN INFLATION-ADJUSTED DOLLARS", s)
    return re.sub(r"\s+", " ", s)


def _last_segment(label: str) -> str:
    return str(label).split("!!")[-1].strip().rstrip(":").strip()


# ---------------------------------------------------------------------------
# Data source: frozen fixtures or live Census endpoints
# ---------------------------------------------------------------------------


class SourceError(RuntimeError):
    """A Census request failed, or a fixture is unusable."""


class FixtureMissing(SourceError):
    """Offline mode needs a frozen response that is not in the fixture directory."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def redact(text: Any, key: str = "") -> str:
    """Remove an API key from text (URLs in error messages carry it)."""
    out = "" if text is None else str(text)
    if key:
        out = out.replace(key, "REDACTED")
    return re.sub(r"(?i)(key=)[^&\s'\"]+", r"\1REDACTED", out)


def public_url(endpoint: str, params: Mapping[str, Any]) -> str:
    """Request URL with the API key removed and parameters in a stable order."""
    clean = [(k, v) for k, v in sorted(params.items()) if str(k).lower() != "key"]
    return endpoint + ("?" + urlencode(clean) if clean else "")


def _geo_columns(geo: Mapping[str, str]) -> List[str]:
    cols: List[str] = []
    for part in (geo.get("in", ""), geo.get("for", "")):
        for clause in str(part).split(" "):
            if ":" in clause:
                cols.append(clause.split(":", 1)[0])
    return cols


def _geoid(df: pd.DataFrame, geo_cols: Sequence[str]) -> pd.Series:
    if list(geo_cols) == ["us"]:
        return pd.Series(["US"] * len(df), index=df.index)
    out = pd.Series([""] * len(df), index=df.index)
    for c in geo_cols:
        out = out + df[c].astype(str)
    return out


class CensusSource:
    """Census responses from frozen fixtures (offline) or the live services.

    ``live=False``: every request is answered from ``fixture_dir``; a missing
    fixture raises :class:`FixtureMissing`. ``live=True``: requests go to
    api.census.gov (via ``client``, a :class:`census.api.CensusAPIClient`,
    whose key comes from ``CENSUS_API_KEY``) and www2.census.gov;
    ``record=True`` also writes each response as a fixture with a sidecar.
    """

    def __init__(
        self,
        live: bool = False,
        fixture_dir: Optional[str] = None,
        client: Any = None,
        record: bool = False,
        www2_delay: float = WWW2_DELAY_SECONDS,
    ) -> None:
        self.live = bool(live)
        self.fixture_dir = fixture_dir or DEFAULT_FIXTURE_DIR
        self.record = bool(record) and self.live
        self.www2_delay = float(www2_delay)
        self.client: Any = None
        self._key = ""
        if self.live:
            self.client = client if client is not None else CensusAPIClient(cache=None)
            self._key = str(getattr(self.client, "api_key", "") or "")
        self.urls: List[str] = []
        self._last_www2 = 0.0
        self._memo: Dict[str, Any] = {}
        self.today = _dt.date.today().isoformat()

    @property
    def mode(self) -> str:
        return "live" if self.live else "offline (frozen fixtures)"

    def redact(self, text: Any) -> str:
        return redact(text, self._key)

    # -- fixture files ------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.fixture_dir, name)

    def read_fixture(self, name: str) -> bytes:
        path = self._path(name)
        if not os.path.exists(path):
            raise FixtureMissing(
                f"No frozen fixture {name} in {self.fixture_dir}. Re-record with "
                "`python scripts/validate_against_census.py --live --record`."
            )
        with open(path, "rb") as f:
            raw = f.read()
        sidecar = path + ".json"
        if os.path.exists(sidecar):
            with open(sidecar, encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("sha256") and meta["sha256"] != _sha256(raw):
                raise SourceError(f"Fixture {name} does not match the SHA-256 in its sidecar.")
        return raw

    def fixture_meta(self, name: str) -> Dict[str, Any]:
        sidecar = self._path(name) + ".json"
        if not os.path.exists(sidecar):
            return {}
        with open(sidecar, encoding="utf-8") as f:
            return json.load(f)

    def write_fixture(self, name: str, raw: bytes, meta: Dict[str, Any]) -> None:
        os.makedirs(self.fixture_dir, exist_ok=True)
        with open(self._path(name), "wb") as f:
            f.write(raw)
        meta = dict(meta)
        meta.setdefault("retrieved", self.today)
        meta["sha256"] = _sha256(raw)
        meta["size_bytes"] = len(raw)
        text = json.dumps(meta, indent=2, sort_keys=True)
        if self._key and self._key in text:  # defensive: never persist the key
            raise SourceError("refusing to write a sidecar that contains the API key")
        with open(self._path(name) + ".json", "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")

    # -- api.census.gov -----------------------------------------------------

    @staticmethod
    def api_fixture_name(endpoint: str, params: Mapping[str, Any]) -> Tuple[str, str]:
        url = public_url(endpoint, params)
        tail = endpoint.replace(BASE_URL, "").strip("/")
        slug = re.sub(r"[^0-9A-Za-z]+", "_", tail).strip("_")[:48]
        return f"api_{slug}_{_sha256(url.encode('utf-8'))[:12]}.json.gz", url

    def get_json(self, endpoint: str, params: Mapping[str, Any]) -> Any:
        """One API response (parsed JSON), from a fixture or the live API."""
        name, url = self.api_fixture_name(endpoint, params)
        if url not in self.urls:
            self.urls.append(url)
        if name in self._memo:
            return self._memo[name]
        if not self.live:
            payload = json.loads(gzip.decompress(self.read_fixture(name)).decode("utf-8"))
            self._memo[name] = payload
            return payload
        send = {k: v for k, v in params.items() if str(k).lower() != "key"}
        if self._key:
            send["key"] = self._key
        payload = self.client._get_json(endpoint, send)
        if payload is None:
            raise SourceError(
                f"Census API request failed for {url}: {self.redact(getattr(self.client, 'last_error', ''))}"
            )
        if self.record:
            text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.write_fixture(
                name,
                gzip.compress(text, mtime=0),
                {
                    "kind": "census_api_response",
                    "source_url": url,
                    "uncompressed_sha256": _sha256(text),
                    "note": "Parsed API JSON re-serialized compactly (content identical) and "
                    "gzip-compressed with mtime=0. The API key is not part of the URL.",
                },
            )
        self._memo[name] = payload
        return payload

    def api_frame(
        self, year: int, dataset: str, variables: Sequence[str], geo: Mapping[str, str]
    ) -> pd.DataFrame:
        """Explicit variables (chunked to the 50-variable limit) as one string frame + GEOID."""
        endpoint = f"{BASE_URL}/{year}/{DATASETS[dataset]}"
        variables = list(dict.fromkeys(variables))
        chunks = [variables[i : i + MAX_GET_VARIABLES] for i in range(0, len(variables), MAX_GET_VARIABLES)]
        geo_cols = _geo_columns(geo)
        merged: Optional[pd.DataFrame] = None
        for i, chunk in enumerate(chunks or [[]]):
            get = ",".join((["NAME"] if i == 0 else []) + list(chunk))
            payload = self.get_json(endpoint, {"get": get, **geo})
            df = pd.DataFrame(payload[1:], columns=payload[0])
            df = df.loc[:, ~df.columns.duplicated()]
            df["GEOID"] = _geoid(df, geo_cols)
            if merged is None:
                merged = df
            else:
                merged = merged.merge(df.drop(columns=[c for c in geo_cols if c in df]), on="GEOID", how="outer")
        assert merged is not None
        return merged.reset_index(drop=True)

    def api_group(self, year: int, dataset: str, table: str, geo: Mapping[str, str]) -> pd.DataFrame:
        """``get=NAME,group(TABLE)`` (any number of variables) as a string frame + GEOID."""
        endpoint = f"{BASE_URL}/{year}/{DATASETS[dataset]}"
        payload = self.get_json(endpoint, {"get": f"NAME,group({table})", **geo})
        df = pd.DataFrame(payload[1:], columns=payload[0])
        df = df.loc[:, ~df.columns.duplicated()]
        df["GEOID"] = _geoid(df, _geo_columns(geo))
        return df

    # -- www2.census.gov ----------------------------------------------------

    def _www2_wait(self) -> None:
        wait = self.www2_delay - (time.monotonic() - self._last_www2)
        if wait > 0:
            time.sleep(wait)
        self._last_www2 = time.monotonic()

    def vre_frame(
        self,
        table: str,
        year: int,
        states: Sequence[str],
        sumlev: str = "040",
        variables: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """VRE replicate frame for a state-nested summary level (040 state, 050 county).

        Live: the whole national file (cached by census.replicate in
        .cache/vre). Recording keeps, byte-for-byte, the header, the two title
        rows, and the rows of ``states`` (restricted to ``variables`` when
        given). Offline: that subset.
        """
        name = f"vre_{year}_{sumlev}_{table}.csv.gz"
        key = "vre:" + name
        if key in self._memo:
            return self._memo[key]
        if not self.live:
            frame = _replicate.read_vre_csv(self.read_fixture(name))
            frame.attrs["source_url"] = self.fixture_meta(name).get("source_url", "")
            frame.attrs["scope"] = "fixture subset: " + self.fixture_meta(name).get("selection", "")
            self._memo[key] = frame
            return frame
        url = _replicate.vre_url(table, year, sumlev)
        cache_path = os.path.join(_replicate.vre_cache_dir(year), sumlev, url.rsplit("/", 1)[-1])
        if _replicate._read_cached(cache_path) is None:
            self._www2_wait()
        frame = _replicate.fetch_vre(table, year, sumlev, check_moe=False)
        if url not in self.urls:
            self.urls.append(url)
        frame.attrs["scope"] = f"full national file (summary level {sumlev})"
        if self.record:
            import csv

            with open(frame.attrs["cache_path"], "rb") as f:
                source_raw = f.read()
            data = _replicate._decompress(source_raw)
            wanted_vars = {str(v).upper() for v in variables} if variables else None
            kept: List[bytes] = []
            for i, line in enumerate(data.splitlines(keepends=True)):
                if i == 0:
                    kept.append(line)
                    continue
                fields = next(csv.reader([line.decode("latin-1")]))
                geoid = fields[1].strip() if len(fields) > 1 else ""
                if geoid == "":
                    kept.append(line)
                    continue
                local = geoid.split("US", 1)[1] if "US" in geoid else ""
                if not any(local.startswith(st) for st in states):
                    continue
                if wanted_vars is not None:
                    try:
                        var = f"{fields[0].strip().upper()}_{int(fields[3]):03d}"
                    except (IndexError, ValueError):
                        continue
                    if var not in wanted_vars:
                        continue
                kept.append(line)
            self.write_fixture(
                name,
                gzip.compress(b"".join(kept), mtime=0),
                {
                    "kind": "vre_subset",
                    "source_url": url,
                    "source_sha256": _sha256(source_raw),
                    "source_size_bytes": len(source_raw),
                    "selection": f"summary level {sumlev}: header, the table title/universe rows, and the "
                    f"rows of states {','.join(states)}"
                    + (f" for variables {','.join(sorted(wanted_vars))}" if wanted_vars else ""),
                    "data_rows": len(kept) - 1,
                    "transformation": "selected lines copied byte-for-byte from the decompressed "
                    "source CSV; gzip-compressed with mtime=0",
                },
            )
        self._memo[key] = frame
        return frame

    def summary_file_rows(self, year: int, table: str, geo_ids: Sequence[str]) -> Dict[str, Any]:
        """Header and rows (by GEO_ID) of a table-based Summary File, read by byte range.

        Rows are sorted by GEO_ID, so the nation and state rows sit near the
        start of each file; only the first chunk(s) are requested (HTTP Range),
        never the whole file.
        """
        name = f"sf_{year}_{table.lower()}_rows.txt.gz"
        url = SUMMARY_FILE_URL.format(year=year, table=table.lower())
        if url not in self.urls:
            self.urls.append(url)
        if not self.live:
            text = gzip.decompress(self.read_fixture(name)).decode("utf-8")
            meta = self.fixture_meta(name)
            return _parse_sf(text.splitlines(), geo_ids, meta.get("byte_range", ""))
        import requests  # local: keeps the module importable offline without network deps

        fetched = b""
        total = None
        while True:
            self._www2_wait()
            start = len(fetched)
            resp = requests.get(
                url,
                headers={"Range": f"bytes={start}-{start + SUMMARY_FILE_CHUNK - 1}", "User-Agent": USER_AGENT},
                timeout=120,
            )
            body = resp.content or b""
            if b"Request Rejected" in body[:2000]:
                raise SourceError(f"census.gov rejected the request for {url} (firewall page); retry later.")
            if resp.status_code == 404:
                raise SourceError(f"Not published (HTTP 404): {url}")
            if resp.status_code not in (200, 206):
                raise SourceError(f"HTTP {resp.status_code} for {url}")
            if resp.status_code == 200 and len(body) > SUMMARY_FILE_MAX_BYTES:
                raise SourceError(f"{url} ignored the byte range; refusing a {len(body)}-byte download.")
            m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
            total = int(m.group(1)) if m else total
            fetched += body
            lines = fetched.split(b"\n")[:-1]
            found = {ln.split(b"|", 1)[0].decode("latin-1") for ln in lines[1:]}
            if set(geo_ids) <= found or resp.status_code == 200 or len(body) < SUMMARY_FILE_CHUNK:
                break
            if len(fetched) >= SUMMARY_FILE_MAX_BYTES:
                break
        lines = [ln.decode("latin-1").rstrip("\r") for ln in fetched.split(b"\n")[:-1]]
        byte_range = f"bytes=0-{len(fetched) - 1}" + (f" of {total}" if total else "")
        if self.record:
            wanted = set(geo_ids)
            kept = [lines[0]] + [ln for ln in lines[1:] if ln.split("|", 1)[0] in wanted]
            self.write_fixture(
                name,
                gzip.compress(("\n".join(kept) + "\n").encode("utf-8"), mtime=0),
                {
                    "kind": "summary_file_rows",
                    "source_url": url,
                    "byte_range": byte_range,
                    "fetched_prefix_sha256": _sha256(fetched),
                    "selection": "header line and the rows whose GEO_ID is one of " + ",".join(geo_ids),
                    "transformation": "lines copied verbatim from the fetched prefix; gzip mtime=0",
                },
            )
        return _parse_sf(lines, geo_ids, byte_range)


def _parse_sf(lines: Sequence[str], geo_ids: Sequence[str], byte_range: str) -> Dict[str, Any]:
    header = lines[0].split("|")
    rows: Dict[str, Dict[str, str]] = {}
    wanted = set(geo_ids)
    for ln in lines[1:]:
        parts = ln.split("|")
        if parts[0] in wanted:
            rows[parts[0]] = dict(zip(header, parts))
    return {"header": header, "rows": rows, "byte_range": byte_range}


class FixtureBackedClient(CensusAPIClient):
    """A CensusAPIClient whose responses come from a :class:`CensusSource`.

    Used to run census.pipeline.run_batch unchanged (F1). Its own api_key is
    empty, so recorded requests never contain the key; the source adds the key
    to live requests.
    """

    def __init__(self, source: CensusSource) -> None:
        super().__init__(api_key="", cache=None)
        self._source = source

    def _get_json(self, endpoint: str, params: Dict[str, str]) -> Optional[Any]:
        try:
            payload = self._source.get_json(endpoint, params)
        except FixtureMissing:
            raise
        except SourceError as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        return payload


# ---------------------------------------------------------------------------
# F1 + F9: pipeline integrity (and the continuity report it produces)
# ---------------------------------------------------------------------------


def _f1_entities(b: Battery) -> List[Any]:
    from census.pipeline import build_entities

    return build_entities("county", b.f1_state) + build_entities(
        "county", b.f1_extra_state, list(b.f1_extra_counties)
    )


def _f1_run(source: CensusSource, b: Battery, ctx: Dict[str, Any], fmt: str) -> Dict[str, Any]:
    """run_batch once per output format; cached in ctx (F9 reuses it)."""
    key = f"f1_run_{fmt}"
    if key in ctx:
        return ctx[key]
    from census.pipeline import ResearchOptions, run_batch

    out_dir = tempfile.mkdtemp(prefix=f"census_validation_f1_{fmt}_")
    ctx.setdefault("_tempdirs", []).append(out_dir)
    client = FixtureBackedClient(source)
    options = ResearchOptions(use_cache=False, include_codebook=True)
    result = run_batch(
        b.f1_table, _f1_entities(b), list(b.releases), out_dir, output_format=fmt, options=options, client=client
    )
    ctx[key] = {"result": result, "dir": out_dir, "client": client}
    return ctx[key]


def check_f1_pipeline_integrity(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    ctx = ctx if ctx is not None else {}
    b = battery
    res = ValidationResult(
        "F1",
        f"Every cell of run_batch output ({b.f1_table}, county level, releases "
        f"{', '.join(map(str, b.releases))}, concordance on; tidy CSV panel and Parquet) equals the "
        "raw API cell for the same GEOID, year and source code (Census sentinels -> missing, "
        "except controlled MOEs -> 0 with <base>_CONTROLLED = True).",
        tolerance="exact (float equality); sentinel codes and non-numeric cells must be missing in the output; "
        "a controlled MOE (-555555555) must be 0 and flagged in its <base>_CONTROLLED column",
    )
    res.census_source.append(f"{BASE_URL}/<year>/acs/acs5?get=NAME,group({b.f1_table})&for=county:...")
    failures: List[Dict[str, Any]] = []
    n_compared = 0
    formats_detail: Dict[str, Any] = {}
    for fmt in ("panel", "parquet"):
        run = _f1_run(source, b, ctx, fmt)
        r = run["result"]
        if not r.get("success"):
            res.failures.append({"format": fmt, "error": r.get("error_code"), "message": r.get("message")})
            res.n_failed += 1
            continue
        path = r["panel_path"]
        panel = pd.read_parquet(path) if fmt == "parquet" else pd.read_csv(path, dtype=str)
        codebook = pd.read_csv(r["codebook_path"], dtype=str).fillna("")
        id_slugs = {row["Variable_Code"]: row["Variable_Slug"] for _, row in codebook.iterrows()
                    if row["Variable_Code"] in ("GEOID", "Year", "NAME") and row["Variable_Slug"]}
        panel = panel.rename(columns={v: k for k, v in id_slugs.items()})
        panel["GEOID"] = panel["GEOID"].astype(str).str.zfill(5)
        slug_to_codes: Dict[str, Tuple[str, Dict[int, str]]] = {}
        # Controlled-MOE flag columns (Round 9, G1): <base>_CONTROLLED -> slug.
        controlled_slugs = {row["Variable_Code"][: -len("_CONTROLLED")]: row["Variable_Slug"]
                            for _, row in codebook.iterrows()
                            if str(row["Variable_Code"]).endswith("_CONTROLLED") and row["Variable_Slug"]}
        slug_base: Dict[str, str] = {}
        for _, row in codebook.iterrows():
            parsed = parse_variable_code(row["Variable_Code"])
            if parsed and parsed[1] == "M":
                slug_base[row["Variable_Slug"]] = parsed[0]
            if not parsed or parsed[1] not in ("E", "M"):
                continue
            by_year = {}
            for part in str(row.get("Source_Codes_By_Year", "")).split(";"):
                if ":" in part:
                    y, c = part.split(":", 1)
                    by_year[int(y)] = c
            slug_to_codes[row["Variable_Slug"]] = (parsed[1], by_year)

        raw_frames: Dict[int, pd.DataFrame] = {}
        for year in b.releases:
            parts = []
            for ent in _f1_entities(b):
                endpoint, params = run["client"].build_request(
                    year=year, table_code=b.f1_table, dataset="acs5", **ent.request_kwargs()
                )
                payload = source.get_json(endpoint, params)
                df = pd.DataFrame(payload[1:], columns=payload[0])
                df = df.loc[:, ~df.columns.duplicated()]
                df["GEOID"] = df["state"].astype(str) + df["county"].astype(str)
                parts.append(df)
            raw_frames[year] = pd.concat(parts, ignore_index=True).set_index("GEOID")

        # Row coverage: no lost or invented (GEOID, Year) rows.
        panel_keys = set(zip(panel["GEOID"].astype(str), panel["Year"].astype(int)))
        raw_keys = {(g, y) for y, f in raw_frames.items() for g in f.index}
        lost, extra = sorted(raw_keys - panel_keys), sorted(panel_keys - raw_keys)
        for key in lost[:5]:
            failures.append({"format": fmt, "problem": "row missing from output", "GEOID": key[0], "year": key[1]})
        for key in extra[:5]:
            failures.append({"format": fmt, "problem": "row not in API response", "GEOID": key[0], "year": key[1]})
        res.n_failed += len(lost) + len(extra)

        # Column coverage: every raw E/M code of the table is in the output.
        mapped = {(y, c + s) for s, by in slug_to_codes.values() for y, c in by.items()}
        unmapped = []
        for year, f in raw_frames.items():
            for col in f.columns:
                p = parse_variable_code(col)
                if p and p[1] in ("E", "M") and p[0].startswith(b.f1_table + "_") and (year, col) not in mapped:
                    unmapped.append(f"{year}:{col}")
        if unmapped:
            res.n_failed += len(unmapped)
            failures.append({"format": fmt, "problem": "raw columns absent from output", "columns": unmapped[:20]})

        cells = 0
        mism = 0
        names_bad = 0
        n_controlled = 0
        for _, prow in panel.iterrows():
            year = int(prow["Year"])
            geoid = str(prow["GEOID"])
            raw = raw_frames[year]
            if geoid not in raw.index:
                continue
            if str(prow.get("NAME", "")) != str(raw.at[geoid, "NAME"]):
                names_bad += 1
            for slug, (suffix, by_year) in slug_to_codes.items():
                if year not in by_year or slug not in panel.columns:
                    continue
                code = by_year[year] + suffix
                expected = _numeric([raw.at[geoid, code]])[0] if code in raw.columns else math.nan
                is_controlled = (suffix == "M" and code in raw.columns
                                 and _raw_float(raw.at[geoid, code]) == CONTROLLED_MOE)
                if is_controlled:
                    expected = 0.0  # documented representation: MOE 0 + controlled flag
                got = _raw_float(prow[slug])
                cells += 1
                same = (math.isnan(expected) and math.isnan(got)) or expected == got
                if is_controlled and same:
                    flag_slug = controlled_slugs.get(slug_base.get(slug, ""))
                    flag = prow.get(flag_slug) if flag_slug in panel.columns else None
                    same = str(flag).strip().lower() in ("true", "1", "1.0")
                    n_controlled += 1
                if not same:
                    mism += 1
                    if len(failures) < MAX_FAILURE_SAMPLES:
                        failures.append(
                            {"format": fmt, "GEOID": geoid, "year": year, "column": slug, "source_code": code,
                             "api_value": raw.at[geoid, code] if code in raw.columns else None, "output_value": got}
                        )
        res.n_failed += mism + names_bad
        n_compared += cells
        formats_detail[fmt] = {
            "output_file": os.path.basename(path),
            "rows": int(len(panel)),
            "cells_compared": cells,
            "mismatched_cells": mism,
            "name_mismatches": names_bad,
            "rows_missing": len(lost),
            "rows_extra": len(extra),
            "raw_columns_unmapped": len(unmapped),
            "controlled_moe_cells": n_controlled,
            "concordance_issue_count": r.get("concordance_issue_count"),
        }
    res.n_compared = n_compared
    res.failures = res.failures + failures[:MAX_FAILURE_SAMPLES]
    res.max_abs_error = 0.0 if res.n_failed == 0 else math.nan
    res.details["formats"] = formats_detail
    res.details["entities"] = [e.name for e in _f1_entities(b)]
    res.notes.append(
        "Responses pass through run_batch unchanged via a client whose _get_json is served by the "
        "same source as the comparison, so the check isolates parsing, sentinel cleaning, "
        "concordance renaming, panel stacking, slug renaming and file export."
    )
    return res.finish(res.n_failed == 0 and n_compared > 0)


def check_f9_continuity(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    ctx = ctx if ctx is not None else {}
    b = battery
    res = ValidationResult(
        "F9",
        f"Round 6 national continuity check (census.continuity) for every multi-vintage concept "
        f"of the F1 extraction ({b.f1_table}, {', '.join(map(str, b.releases))}).",
        tolerance="as Round 6: no concept flagged as both significantly (z-test, 90%) and "
        "implausibly (>25% relative or >10 points) different at the national level",
    )
    run = _f1_run(source, b, ctx, "panel")
    r = run["result"]
    if not r.get("success"):
        res.notes.append(f"F1 extraction failed: {r.get('message')}")
        return res.finish(False)
    with open(r["issues_path"], encoding="utf-8") as f:
        issues = json.load(f)
    cont = issues.get("continuity") or {}
    res.census_source.append(f"{BASE_URL}/<year>/acs/acs5?get=NAME,group({b.f1_table})&for=us:1")
    flagged = {cid: v for cid, v in cont.items() if v.get("flags") and not v.get("exemption")}
    res.n_compared = sum(int(v.get("checked_pairs", 0)) for v in cont.values())
    res.n_failed = len(flagged)
    res.failures = [{"concept": cid, "flags": v["flags"]} for cid, v in list(flagged.items())[:MAX_FAILURE_SAMPLES]]
    res.details = {
        "concepts_checked": len(cont),
        "concepts_passed": sum(1 for v in cont.values() if v.get("passed")),
        "concepts_flagged": len(flagged),
        "concepts_missing_years": sum(1 for v in cont.values() if v.get("missing_years")),
    }
    res.notes.append(
        "A pass shows no national value jumps implausibly between vintages; it does not prove the "
        "codes measure the same thing (census/continuity.py docstring)."
    )
    info_out: Dict[str, Any] = {}
    for table in b.f9_info_tables:
        try:
            from census.pipeline import ResearchOptions, plan_extraction

            plan = plan_extraction(table, _f1_entities(b), list(b.releases), "panel",
                                   ResearchOptions(use_cache=False), None, None, FixtureBackedClient(source))
            info = plan.get("_continuity") or {}
            info_out[table] = {
                "concepts_checked": len(info),
                "concordance_status_counts": (plan.get("concordance_summary") or {}).get("status_counts"),
                "would_block": plan.get("would_block"),
                "flagged": {
                    v["flags"][0]["from"]["code"]: {
                        "from": v["flags"][0]["from"]["estimate"], "to": v["flags"][0]["to"]["estimate"],
                        "relative_change": v["flags"][0]["change"], "z": v["flags"][0]["z_statistic"]}
                    for v in info.values() if v.get("flags")
                },
            }
        except Exception as exc:  # informational only
            info_out[table] = {"error": source.redact(exc)}
    if info_out:
        res.details["informational_not_asserted"] = info_out
        res.notes.append(
            "Informational (not asserted): the Round 6 check flags concepts of "
            + ", ".join(t for t, v in info_out.items() if v.get("flagged"))
            + f" for {'-'.join(map(str, b.releases))} although every concept keeps its code and label. The "
            "flagged series show real change over four years (income brackets in nominal dollars, poverty "
            "and insurance by age as the population ages). census/continuity.py's thresholds (25% relative, "
            "10 points) are documented for adjacent, overlapping releases but are applied to any requested "
            "pair, so such extractions block in the default concordance mode. A false positive of the "
            "Round 6 check, reported for the module owner; it is not a calculation error."
        )
    return res.finish(not flagged and res.n_compared > 0)


# ---------------------------------------------------------------------------
# F2 / F4: Data Profile percentages
# ---------------------------------------------------------------------------


def _verify_construction(con: Construction, year: int) -> Tuple[List[str], Dict[str, Any]]:
    """Check one pairing against that release's labels. Returns (problems, record)."""
    problems: List[str] = []
    acs, _ = _md.vintage_variables("acs5", year)
    prof, _ = _md.vintage_variables("profile", year)
    record: Dict[str, Any] = {"year": year}
    dp = prof.get(con.dp + "PE")
    if dp is None:
        problems.append(f"{con.dp}PE not in the {year} profile metadata")
    else:
        record["dp_label"] = dp["label"]
        if _norm_label(dp["label"]) != _norm_label("Percent!!" + con.dp_label):
            problems.append(f"{con.dp}PE label in {year} is '{dp['label']}', expected '{con.dp_label}'")
    record["universe"] = _md.table_universe(con.table, "acs5", year) or "(not published in this vintage's groups.json)"
    den = acs.get(con.den + "E")
    if den is None or _norm_label(den["label"]) != "TOTAL":
        problems.append(f"{con.den}E is not the table total in {year}")
    labels = {}
    for code in con.num:
        meta = acs.get(code + "E")
        if meta is None:
            problems.append(f"{code}E missing in {year}")
            continue
        labels[code] = meta["label"]
        if _last_segment(meta["label"]) not in con.num_labels:
            problems.append(f"{code}E label '{meta['label']}' is not one of {con.num_labels}")
        try:
            if not _derived.subset_relationship(code + "E", con.den + "E", acs):
                problems.append(f"derived.subset_relationship({code}, {con.den}) is False in {year}")
        except KeyError as exc:
            problems.append(str(exc))
    record["numerator_labels"] = labels
    return problems, record


def _valid_constructions(year: int) -> List[Construction]:
    return [c for c in CONSTRUCTIONS if not _verify_construction(c, year)[0]]


def _dp_frames(source: CensusSource, b: Battery, year: int, cons: Sequence[Construction], level: str) -> pd.DataFrame:
    geo = (
        {"for": f"state:{b.state_list()}"}
        if level == "state"
        else {"for": "county:*", "in": f"state:{b.state_list()}"}
    )
    acs_vars = [f"{c}{s}" for con in cons for c in list(con.num) + [con.den] for s in ("E", "M")]
    dp_vars = [f"{con.dp}{s}" for con in cons for s in ("PE", "PM")]
    acs_df = source.api_frame(year, "acs5", acs_vars, geo)
    dp_df = source.api_frame(year, "profile", dp_vars, geo)
    df = acs_df.merge(dp_df[["GEOID"] + dp_vars], on="GEOID", how="inner")
    for c in acs_vars + dp_vars:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _handbook_proportion_moe(df: pd.DataFrame, con: Construction) -> Tuple[np.ndarray, np.ndarray]:
    """Independent re-implementation of Handbook 2020 ch. 8 formulas 1 and 6 (percent scale).

    Used only to confirm derived.derive_columns implements the documented
    formulas; the comparison with Census is the DP check itself.
    """

    def cell(code: str, suffix: str) -> np.ndarray:
        return pd.to_numeric(df[code + suffix], errors="coerce").to_numpy(float)

    def moe(code: str) -> np.ndarray:
        m = cell(code, "M")
        m = np.where(m == CONTROLLED_MOE, 0.0, m)  # controlled: no sampling error
        return np.where(np.isin(m, list(_SENTINELS)), np.nan, m)

    x = np.sum([cell(c, "E") for c in con.num], axis=0)
    mx = np.sqrt(np.sum([moe(c) ** 2 for c in con.num], axis=0))
    y, my = cell(con.den, "E"), moe(con.den)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = x / y
        rad = mx**2 - p**2 * my**2
        out = np.where(rad >= 0, np.sqrt(np.abs(rad)) / y, np.sqrt(mx**2 + p**2 * my**2) / y)
        out = np.where(p == 1, mx / y, out)
    return p * 100.0, out * 100.0


def check_f2_profile_proportions(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    b = battery
    res = ValidationResult(
        "F2",
        "Data Profile percentages and percent MOEs rebuilt from detailed-table counts with "
        "derived.derive_columns (sum_estimates + proportion, Handbook 2020 ch. 8 formulas 1 and 6), "
        f"states {b.state_list()} and all their counties, releases {', '.join(map(str, b.releases))}.",
        tolerance="estimate: |ours - DP| <= 0.1 percentage point (DP is rounded to 0.1). MOE: our "
        "approximate MOE must equal an independent evaluation of the Handbook formulas (1e-9); for "
        "single-cell numerators (simple subset proportions) it must also be within max(0.2 point, 25%) of "
        "the DP MOE, unless the exact replicate MOE for the same geography reproduces the DP MOE within "
        "0.1 point (then the gap is the approximation's ignored covariance and is counted as explained); "
        "for multi-cell numerators the ratio ours/DP is reported, not asserted",
    )
    res.census_source += [HANDBOOK_CH8, f"{BASE_URL}/<year>/acs/acs5/profile", f"{BASE_URL}/<year>/acs/acs5"]
    failures: List[Dict[str, Any]] = []
    explained: List[Dict[str, Any]] = []
    abs_errs: List[float] = []
    rel_errs: List[float] = []
    n = 0
    n_failed = 0
    formula_bad = 0
    ratios: Dict[str, List[float]] = {}
    per: Dict[str, Dict[str, Any]] = {}
    definitions: Dict[str, Any] = {}
    fallback = 0
    pending: List[Dict[str, Any]] = []  # simple-proportion MOE misses awaiting replicate evidence
    for year in b.releases:
        cons = []
        for con in CONSTRUCTIONS:
            problems, record = _verify_construction(con, year)
            definitions.setdefault(con.id, {"definition": con.definition, "dp": con.dp, "table": con.table,
                                            "numerator": list(con.num), "denominator": con.den, "releases": {}})
            definitions[con.id]["releases"][str(year)] = {**record, "problems": problems}
            if problems:
                res.notes.append(f"{con.id} {year} not compared (validation-design mismatch): {'; '.join(problems)}")
                continue
            cons.append(con)
        for level in ("state", "county"):
            df = _dp_frames(source, b, year, cons, level)  # same requests as F4 (all valid constructions)
            spec = [
                {"name": con.id, "op": "proportion", "den": con.den + "E", "as_percent": True,
                 "num": [c + "E" for c in con.num] if len(con.num) > 1 else con.num[0] + "E"}
                for con in cons
            ]
            out = _derived.derive_columns(df, spec)
            for con in cons:
                pub = _numeric(out[con.dp + "PE"])
                pub_m = _numeric(out[con.dp + "PM"])
                est = out[f"{con.id}_estimate"].to_numpy(float)
                moe = out[f"{con.id}_moe"].to_numpy(float)
                ref_est, ref_moe = _handbook_proportion_moe(df, con)
                fallback += int((out[f"{con.id}_method"] != "proportion").sum())
                stat = per.setdefault(f"{con.id} {year} {level}", {
                    "n": 0, "estimate_failures": 0, "formula_mismatches": 0, "moe_band_misses": 0,
                    "moe_band_misses_explained_by_replicates": 0, "moe_failures": 0, "max_abs_error": 0.0,
                    "skipped": 0})
                for i in range(len(out)):
                    if not (np.isfinite(pub[i]) and np.isfinite(est[i])):
                        stat["skipped"] += 1
                        continue
                    n += 1
                    stat["n"] += 1
                    err = est[i] - pub[i]
                    abs_errs.append(abs(err))
                    if pub[i]:
                        rel_errs.append(abs(err) / abs(pub[i]))
                    stat["max_abs_error"] = max(stat["max_abs_error"], abs(err))
                    bad_est = abs(err) > 0.1 + 1e-9
                    bad_formula = not (
                        np.isclose(est[i], ref_est[i], rtol=1e-9, atol=1e-9)
                        and (np.isclose(moe[i], ref_moe[i], rtol=1e-9, atol=1e-9)
                             or (np.isnan(moe[i]) and np.isnan(ref_moe[i])))
                    )
                    row = {"construction": con.id, "year": year, "level": level, "GEOID": out.at[i, "GEOID"],
                           "NAME": out.at[i, "NAME"], "ours": round(float(est[i]), 4), "published": float(pub[i]),
                           "ours_moe": round(float(moe[i]), 4),
                           "published_moe": float(pub_m[i]) if np.isfinite(pub_m[i]) else None}
                    if np.isfinite(pub_m[i]) and np.isfinite(moe[i]) and pub_m[i] > 0:
                        ratios.setdefault(con.id, []).append(moe[i] / pub_m[i])
                        if con.simple and abs(moe[i] - pub_m[i]) > max(0.2, 0.25 * pub_m[i]) + 1e-9:
                            stat["moe_band_misses"] += 1
                            if not (bad_est or bad_formula):
                                pending.append({**row, "_con": con, "_stat": stat})
                    stat["estimate_failures"] += int(bad_est)
                    stat["formula_mismatches"] += int(bad_formula)
                    formula_bad += int(bad_formula)
                    if bad_est or bad_formula:
                        n_failed += 1
                        if len(failures) < MAX_FAILURE_SAMPLES:
                            failures.append({**row, "failed": "estimate" if bad_est else "formula",
                                             "reference_moe": round(float(ref_moe[i]), 6)})
    # Replicate evidence for simple-proportion MOE misses (same geography, same release).
    groups: Dict[Tuple[str, int, str], List[Dict[str, Any]]] = {}
    for row in pending:
        groups.setdefault((row["_con"].id, row["year"], row["level"]), []).append(row)
    for (cid, year, level), rows in groups.items():
        con = rows[0]["_con"]
        rep = None
        if con.vre:
            try:
                frame = source.vre_frame(con.table, year, b.states, "040" if level == "state" else "050",
                                         list(con.num) + [con.den])
                url = frame.attrs.get("source_url")
                if url and url not in res.census_source:
                    res.census_source.append(url)
                rep = _replicate.replicate_proportion(
                    frame, list(con.num), con.den, geoids=[r["GEOID"] for r in rows], as_percent=True
                ).set_index("GEOID")
            except (SourceError, KeyError, ValueError) as exc:
                res.notes.append(f"replicate evidence unavailable for {cid} {year} {level}: {source.redact(exc)}")
        for row in rows:
            stat = row.pop("_stat")
            row.pop("_con")
            rep_moe = float(rep.at[row["GEOID"], "moe"]) if rep is not None and row["GEOID"] in rep.index else math.nan
            row["replicate_moe"] = round(rep_moe, 4) if np.isfinite(rep_moe) else None
            if np.isfinite(rep_moe) and abs(rep_moe - row["published_moe"]) <= 0.1 + 1e-9:
                stat["moe_band_misses_explained_by_replicates"] += 1
                explained.append(row)
            else:
                stat["moe_failures"] += 1
                n_failed += 1
                if len(failures) < MAX_FAILURE_SAMPLES:
                    failures.append({**row, "failed": "moe (not explained by replicates)"})
    res.n_compared = n
    res.n_failed = n_failed
    res.max_abs_error = _max(abs_errs)
    res.max_rel_error = _max(rel_errs)
    res.failures = failures
    res.details = {
        "pairings": definitions,
        "per_construction": per,
        "moe_ratio_ours_over_published": {k: _distribution(v) for k, v in ratios.items()},
        "formula_mismatches": formula_bad,
        "proportion_ratio_fallbacks": fallback,
        "moe_band_misses_explained_by_replicates": len(explained),
        "explained_sample": explained[:10],
    }
    res.notes.append(
        "Census computes Data Profile MOEs from replicate weights, which capture the covariance between "
        "numerator and denominator; the Handbook approximation ignores it, so it can over- or understate "
        "the MOE (ACS General Handbook 2020, ch. 8). Where the approximate MOE misses the band, the exact "
        "replicate MOE for the same geography is computed: if it reproduces the published MOE, the toolkit's "
        "formula is right and the gap is the documented approximation error."
    )
    return res.finish(n_failed == 0 and n > 0)


def check_f4_replicate_proportions(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    b = battery
    res = ValidationResult(
        "F4",
        "Exact replicate proportions (replicate.replicate_proportion on VRE files) vs Data Profile percents "
        f"and percent MOEs: states {b.state_list()} for every construction with a VRE table, plus all their "
        f"counties for {', '.join(b.f4_county_constructions)}; releases {', '.join(map(str, b.releases))}.",
        tolerance="|ours - DP| <= 0.1 percentage point for both the estimate and the MOE (DP rounded to 0.1)",
    )
    res.census_source += [_replicate.VRE_DOCUMENTATION, f"{BASE_URL}/<year>/acs/acs5/profile"]
    failures: List[Dict[str, Any]] = []
    abs_errs: List[float] = []
    moe_errs: List[float] = []
    approx_ratio: Dict[str, List[float]] = {}
    n = n_failed = 0
    per: Dict[str, Any] = {}
    for year in b.releases:
        valid = _valid_constructions(year)
        for level in ("state", "county"):
            cons = [c for c in valid if c.vre and (level == "state" or c.id in b.f4_county_constructions)]
            if not cons:
                continue
            dp = _dp_frames(source, b, year, valid, level).set_index("GEOID")
            for con in cons:
                frame = source.vre_frame(con.table, year, b.states, "040" if level == "state" else "050",
                                         None if level == "state" else list(con.num) + [con.den])
                url = frame.attrs.get("source_url")
                if url and url not in res.census_source:
                    res.census_source.append(url)
                in_frame = set(frame["GEOID"])
                geoids = [g for g in dp.index if g in in_frame]
                missing = [g for g in dp.index if g not in in_frame]
                if missing:
                    res.notes.append(f"{con.id} {year} {level}: {len(missing)} geographies not in the VRE file")
                out = _replicate.replicate_proportion(frame, list(con.num), con.den, geoids=geoids, as_percent=True)
                approx = _derived.derive_columns(
                    dp.loc[geoids].reset_index(),
                    [{"name": "a", "op": "proportion", "den": con.den + "E", "as_percent": True,
                      "num": [c + "E" for c in con.num] if len(con.num) > 1 else con.num[0] + "E"}],
                )
                stat = per.setdefault(f"{con.id} {year} {level}", {"n": 0, "failures": 0, "max_estimate_error": 0.0,
                                                                     "max_moe_error": 0.0, "skipped": 0})
                for i, g in enumerate(out["GEOID"]):
                    pub, pub_m = _numeric([dp.at[g, con.dp + "PE"]])[0], _numeric([dp.at[g, con.dp + "PM"]])[0]
                    est, moe = float(out.at[i, "estimate"]), float(out.at[i, "moe"])
                    if not (np.isfinite(pub) and np.isfinite(pub_m)):
                        stat["skipped"] += 1
                        continue
                    n += 1
                    stat["n"] += 1
                    e_err, m_err = abs(est - pub), abs(moe - pub_m)
                    abs_errs.append(e_err)
                    moe_errs.append(m_err)
                    stat["max_estimate_error"] = max(stat["max_estimate_error"], e_err)
                    stat["max_moe_error"] = max(stat["max_moe_error"], m_err)
                    if pub_m == 0.1 and moe < 0.05:
                        stat["published_moe_at_display_floor"] = stat.get("published_moe_at_display_floor", 0) + 1
                    if moe > 0:
                        approx_ratio.setdefault(f"{con.id} {level}", []).append(float(approx.at[i, "a_moe"]) / moe)
                    if not (e_err <= 0.1 + 1e-9 and m_err <= 0.1 + 1e-9):
                        n_failed += 1
                        stat["failures"] += 1
                        failures.append({"construction": con.id, "year": year, "level": level, "GEOID": g,
                                         "ours": round(est, 4), "published": pub, "ours_moe": round(moe, 4),
                                         "published_moe": pub_m, "notes": "; ".join(out.at[i, "notes"])})
    res.n_compared = n
    res.n_failed = n_failed
    res.max_abs_error = _max(abs_errs + moe_errs)
    res.failures = failures[:MAX_FAILURE_SAMPLES]
    res.details = {
        "per_construction": per,
        "max_estimate_error": _max(abs_errs),
        "max_moe_error": _max(moe_errs),
        "approximate_over_replicate_moe": {k: _distribution(v) for k, v in approx_ratio.items()},
        "pairings": "same constructions and Census definitions as F2 (bachelors_or_higher excluded: B15003 "
        "has no VRE file)",
    }
    res.notes.append(
        "published_moe_at_display_floor counts rows where the DP shows a percent MOE of 0.1 while the exact "
        "replicate MOE is below 0.05 (it would round to 0.0). This occurs for the 65+ share at the state "
        "level, which is nearly controlled to the population estimates by age; Census does not display a "
        "0.0 percent MOE for an uncontrolled item, so 0.1 acts as a floor. These rows pass only because the "
        "tolerance is 0.1; they are listed so the margin is visible."
    )
    return res.finish(n_failed == 0 and n > 0)


# ---------------------------------------------------------------------------
# F3: replicate MOEs and VRE cells
# ---------------------------------------------------------------------------

LOCAL_VRE_FIXTURES = (
    os.path.join("tests", "fixtures", "vre", "B01001_050_2018_subset.csv.gz"),
    os.path.join("tests", "fixtures", "vre", "B19001_040_2023_subset.csv.zip"),
    os.path.join("tests", "fixtures", "vre", "B19001_050_2014_subset.csv.gz"),
    os.path.join("tests", "fixtures", "medians", "vre_2023_050_B19001_nj.csv.gz"),
    os.path.join("tests", "fixtures", "medians", "vre_2023_050_B25063_nj.csv.gz"),
)


def check_f3_replicate_moe(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    b = battery
    res = ValidationResult(
        "F3",
        "MOEs recomputed from the 80 variance replicates (replicate.verify_published_moe, "
        "variance.replicate_moe) vs the MOE published in the same VRE file; VRE estimates and MOEs vs "
        f"API estimates and MOEs for the same cells. Tables {', '.join(b.f3_tables)}, state level, "
        f"releases {', '.join(map(str, b.releases))}, plus the committed county/state VRE fixtures.",
        tolerance="replicate MOE within 0.5 of the published (integer) MOE, i.e. one rounding unit; "
        "VRE cell == API cell exactly (an API MOE of -555555555 matches a controlled VRE row)",
    )
    res.census_source += [_replicate.VRE_DOCUMENTATION, _replicate.VRE_LANDING_URL]
    failures: List[Dict[str, Any]] = []
    files: Dict[str, Any] = {}
    n = n_failed = 0
    max_diff: List[float] = []
    cells = cells_bad = 0
    for year in b.releases:
        for table in b.f3_tables:
            frame = source.vre_frame(table, year, b.states)
            url = frame.attrs.get("source_url", "")
            if url and url not in res.census_source:
                res.census_source.append(url)
            rep = _replicate.verify_published_moe(frame)
            n += rep["rows_checked"]
            n_failed += rep["n_mismatch"]
            max_diff.append(rep["max_abs_diff"])
            for m in rep["mismatches"][:5]:
                failures.append({"file": f"{table} {year}", **m})
            api = source.api_group(year, "acs5", table, {"for": f"state:{b.state_list()}"}).set_index("GEOID")
            sub = frame[frame["GEOID"].isin(b.states)]
            c_ok = c_bad = controlled_match = 0
            for _, row in sub.iterrows():
                g, var = row["GEOID"], row["variable"]
                for suffix, val in (("E", row["estimate"]), ("M", row["moe"])):
                    col = var + suffix
                    if g not in api.index or col not in api.columns:
                        c_bad += 1
                        failures.append({"file": f"{table} {year}", "GEOID": g, "cell": col, "problem": "not in API response"})
                        continue
                    api_v = _raw_float(api.at[g, col])
                    if suffix == "M" and api_v == CONTROLLED_MOE and bool(row["controlled"]):
                        controlled_match += 1
                        c_ok += 1
                        continue
                    if (math.isnan(api_v) and math.isnan(val)) or api_v == val:
                        c_ok += 1
                    else:
                        c_bad += 1
                        if len(failures) < MAX_FAILURE_SAMPLES:
                            failures.append({"file": f"{table} {year}", "GEOID": g, "cell": col,
                                             "vre_value": val, "api_value": api.at[g, col]})
            cells += c_ok + c_bad
            cells_bad += c_bad
            files[f"{table} {year}"] = {
                "scope": frame.attrs.get("scope", ""),
                "rows": rep["rows"], "rows_checked": rep["rows_checked"],
                "controlled_rows": rep["controlled_rows"], "model_based_rows": rep["model_based_rows"],
                "max_abs_diff": rep["max_abs_diff"], "moe_mismatches": rep["n_mismatch"],
                "api_cells_compared": c_ok + c_bad, "api_cell_mismatches": c_bad,
                "controlled_moe_cells": controlled_match,
            }
    for rel in LOCAL_VRE_FIXTURES:
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(path):
            res.notes.append(f"local VRE fixture {rel} not found; skipped")
            continue
        frame = _replicate.read_vre_csv(path)
        rep = _replicate.verify_published_moe(frame)
        n += rep["rows_checked"]
        n_failed += rep["n_mismatch"]
        max_diff.append(rep["max_abs_diff"])
        for m in rep["mismatches"][:5]:
            failures.append({"file": rel, **m})
        files[rel.replace(os.sep, "/")] = {k: rep[k] for k in ("rows", "rows_checked", "controlled_rows",
                                                               "model_based_rows", "max_abs_diff")}
        files[rel.replace(os.sep, "/")]["moe_mismatches"] = rep["n_mismatch"]
    res.n_compared = n + cells
    res.n_failed = n_failed + cells_bad
    res.max_abs_error = _max(max_diff)
    res.failures = failures[:MAX_FAILURE_SAMPLES]
    res.details = {"files": files, "replicate_rows_checked": n, "api_cells_compared": cells,
                   "api_cell_mismatches": cells_bad}
    res.notes.append(
        "Controlled rows (CME '*****', published MOE 0) and model-based rows (all 80 replicates equal "
        "the estimate, published MOE from the VRE documentation's eqs. 3-5) cannot be reproduced from "
        "replicates and are counted separately, as the VRE documentation prescribes."
    )
    return res.finish(res.n_failed == 0 and n > 0)


# ---------------------------------------------------------------------------
# F5: medians
# ---------------------------------------------------------------------------

MEDIAN_PAIRS = (
    ("B25063", "B25064_001", "Median gross rent (B25064) from the gross rent distribution (B25063, "
     "24 cash-rent brackets; 'No cash rent' excluded). Census computes B25064 from the same brackets."),
    ("B19001", "B19013_001", "Median household income (B19013) from household income brackets (B19001, "
     "16 brackets). Census interpolates a finer internal distribution ($2,500 intervals below $250,000; "
     "Subject Definitions 2023, 'Median Income'), so exact agreement is not expected."),
)


def _gap_midpoint_diagnosis(row: pd.Series, brackets: Sequence[Any], published: float) -> Optional[str]:
    """Explain a median that sits exactly on a bracket boundary followed by empty brackets.

    When the cumulative count reaches exactly half the total at the top of a
    bracket and the next brackets are empty, every value in the empty gap is a
    median; Census publishes the midpoint of the gap, linear interpolation the
    gap's lower edge. Returns a description when that is the case.
    """
    counts = np.array([_raw_float(row[b[0] + "E"]) for b in brackets])
    total = counts.sum()
    cum = np.cumsum(counts)
    hit = np.flatnonzero(np.isclose(cum, total / 2.0))
    if not hit.size:
        return None
    k = int(hit[0])
    nxt = next((j for j in range(k + 1, len(brackets)) if counts[j] > 0), None)
    if nxt is None or nxt == k + 1:
        return None
    lo, hi = brackets[k][2], brackets[nxt][1]
    mid = (lo + hi) / 2.0
    if not math.isclose(mid, published, abs_tol=1.0):
        return None
    return (
        f"cumulative count is exactly 50% at {lo:g}; brackets up to {hi:g} are empty; Census publishes the gap "
        f"midpoint {mid:g}, linear interpolation returns the gap's lower edge {lo:g}"
    )


def check_f5_medians(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    b = battery
    res = ValidationResult(
        "F5",
        "Medians interpolated from published distribution tables (medians.median_for_rows, linear "
        f"interpolation) vs published medians, all counties of states {b.state_list()}, releases "
        f"{', '.join(map(str, b.releases))}.",
        tolerance="gross rent: every county within max($1, 0.1%) of B25064. Household income: median "
        f"|relative error| < 1% and >= {INCOME_MEDIAN_MIN_SHARE_IN_MOE:.0%} of counties within the published "
        "B19013 MOE (share within 2% reported, not asserted: B19001 is coarser than the distribution Census "
        "interpolates)",
    )
    res.census_source += [SUBJECT_DEFINITIONS_2023, f"{BASE_URL}/<year>/acs/acs5"]
    failures: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {}
    n = n_failed = 0
    abs_all: List[float] = []
    rel_all: List[float] = []
    ok = True
    geo = {"for": "county:*", "in": f"state:{b.state_list()}"}
    for year in b.releases:
        for table, pub_code, definition in MEDIAN_PAIRS:
            brackets = _medians.distribution_brackets(table, year)
            codes = [br[0] for br in brackets]
            variables = [c + "E" for c in codes] + [c + "M" for c in codes] + [pub_code + "E", pub_code + "M"]
            df = source.api_frame(year, "acs5", variables, geo)
            out = _medians.median_for_rows(df, table, year)
            pub = _numeric(df[pub_code + "E"])
            pub_m = _numeric(df[pub_code + "M"])
            ours = out["estimate"].to_numpy(float)
            top_lower = brackets[-1][1]
            bottom_upper = brackets[0][2]
            coded = (
                out["top_coded"].to_numpy(bool) | out["bottom_coded"].to_numpy(bool)
                | (pub >= top_lower) | (pub <= bottom_upper)
            )
            valid = np.isfinite(pub) & np.isfinite(ours) & ~coded & (pub > 0)
            err = ours - pub
            rel = np.abs(err) / np.where(pub > 0, pub, np.nan)
            key = f"{table}->{pub_code} {year}"
            e, r = np.abs(err[valid]), rel[valid]
            n += int(valid.sum())
            abs_all += list(e)
            rel_all += list(r)
            moe_ratio = (out["moe"].to_numpy(float) / pub_m)[valid & (pub_m > 0)]
            entry: Dict[str, Any] = {
                "definition": definition,
                "counties": int(len(df)),
                "compared": int(valid.sum()),
                "excluded_top_or_bottom_coded_or_missing": int((~valid).sum()),
                "max_abs_error": float(e.max()) if e.size else None,
                "median_abs_error": float(np.median(e)) if e.size else None,
                "max_rel_error": float(r.max()) if r.size else None,
                "median_rel_error": float(np.median(r)) if r.size else None,
                "share_within_2pct": float((r <= 0.02).mean()) if r.size else None,
                "approx_moe_over_published_moe": _distribution(moe_ratio),
            }
            if table == "B25063":
                bad = valid & (np.abs(err) > np.maximum(1.0, 0.001 * np.where(pub > 0, pub, 0)) + 1e-9)
                entry["share_within_1_dollar"] = float((e <= 1.0 + 1e-9).mean()) if e.size else None
                entry["failures"] = int(bad.sum())
                n_failed += int(bad.sum())
                ok = ok and not bad.any()
                gap_cases = 0
                for i in np.flatnonzero(bad):
                    diag = _gap_midpoint_diagnosis(df.iloc[i], brackets, float(pub[i]))
                    gap_cases += int(diag is not None)
                    if len(failures) < MAX_FAILURE_SAMPLES:
                        failures.append({"pair": key, "GEOID": df.at[i, "GEOID"], "NAME": df.at[i, "NAME"],
                                         "ours": round(float(ours[i]), 2), "published": float(pub[i]),
                                         "diagnosis": diag or "not diagnosed"})
                entry["failures_explained_by_empty_bracket_gap_midpoint"] = gap_cases
            else:
                within = (r <= 0.02)
                with np.errstate(invalid="ignore"):
                    in_moe = (np.abs(err) <= pub_m)[valid & np.isfinite(pub_m)]
                # Criterion (revised 2026-11-02 after the first live run, see PLAN_ROUND_7 16a):
                # B19001 is coarser than Census's internal $2,500 distribution, so a
                # county-level 2% band measures bracket coarseness, not calculation
                # error. A calculation error would push medians outside Census's own
                # sampling error, so assert median error < 1% and >= 98% of counties
                # within the published MOE. The 2% share is still reported.
                share_moe = float(in_moe.mean()) if in_moe.size else 0.0
                passed = bool(r.size) and float(np.median(r)) < 0.01 and share_moe >= INCOME_MEDIAN_MIN_SHARE_IN_MOE
                n_failed += int(not passed)
                entry["counties_outside_2pct"] = int((~within).sum())
                entry["criterion"] = (
                    f"median relative error < 1% and >= {INCOME_MEDIAN_MIN_SHARE_IN_MOE:.0%} of counties "
                    "within the published MOE (2% share reported, not asserted)"
                )
                entry["criterion_met"] = passed
                entry["share_within_published_moe"] = float(in_moe.mean()) if in_moe.size else None
                entry["outside_2pct_but_within_published_moe"] = int(
                    ((rel > 0.02) & (np.abs(err) <= pub_m) & valid).sum())
                ok = ok and passed
                worst = np.argsort(-np.where(valid, rel, -1))[:5]
                for i in worst:
                    if valid[i] and rel[i] > 0.02:
                        failures.append({"pair": key, "GEOID": df.at[i, "GEOID"], "NAME": df.at[i, "NAME"],
                                         "ours": round(float(ours[i]), 2), "published": float(pub[i]),
                                         "rel_error": round(float(rel[i]), 4), "note": "outside 2% (reported; criterion is the published MOE)"})
            stats[key] = entry
    res.n_compared = n
    res.n_failed = n_failed
    res.max_abs_error = _max(abs_all)
    res.max_rel_error = _max(rel_all)
    res.failures = failures[:MAX_FAILURE_SAMPLES]
    res.details = {"pairs": stats}
    res.notes.append(
        "n_failed counts gross-rent counties outside the per-county bound plus household-income "
        "releases whose distribution criterion is not met (median relative error < 1% and >= 98% of "
        "counties within the published MOE; counties outside 2% are listed as counties_outside_2pct). Published medians at a top or bottom coding bound "
        "and medians we top/bottom-code are excluded."
    )
    return res.finish(ok and n > 0)


# ---------------------------------------------------------------------------
# F6 / F7: Comparison Profile
# ---------------------------------------------------------------------------


def _cp_items(source: CensusSource, b: Battery, late: int, early: int) -> Dict[str, Any]:
    """Pair every CP03 item with its DP03 item in the early and late release."""
    geo = {"for": f"state:{b.state_list()}"}
    cp = source.api_group(late, "cprofile", "CP03", geo).set_index("GEOID")
    dp_late = source.api_group(late, "profile", "DP03", geo).set_index("GEOID")
    dp_early = source.api_group(early, "profile", "DP03", geo).set_index("GEOID")
    cp_meta, _ = _md.table_variables("CP03", "cprofile", late)
    meta_late, _ = _md.table_variables("DP03", "profile", late)
    meta_early, _ = _md.table_variables("DP03", "profile", early)

    def index(meta: Mapping[str, Any]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for code, m in meta.items():
            if re.match(r"^DP03_\d{4}(E|PE)$", code):
                out.setdefault(_norm_label(m["label"]), []).append(code)
        return out

    idx_late, idx_early = index(meta_late), index(meta_early)
    items: List[Dict[str, Any]] = []
    unmatched: List[str] = []
    for code in sorted(cp_meta):
        m = re.match(rf"^CP03_{early}_(\d{{3}})E$", code)
        if not m:
            continue
        num = m.group(1)
        late_code = f"CP03_{late}_{num}E"
        ss_code = f"CP03_{late}to{early}_{num}SS"
        label = _norm_label(cp_meta[code]["label"])
        if late_code not in cp_meta or late_code not in cp.columns or code not in cp.columns:
            unmatched.append(code)
            continue

        def base(idx: Dict[str, List[str]]) -> Optional[str]:
            cands = sorted({re.sub(r"P?E$", "", c) for c in idx.get(label, [])})
            if len(cands) == 1:
                return cands[0]
            same = f"DP03_0{num}"
            return same if same in cands else None

        b_late, b_early = base(idx_late), base(idx_early)
        if b_late is None or b_early is None:
            unmatched.append(code)
            continue
        # Which DP value does the CP show? Decide from the later period, where CP == DP.
        cp_late = _numeric(cp[late_code])
        suffix = None
        for e_suf in ("PE", "E"):
            col = b_late + e_suf
            if col in dp_late.columns:
                dv = _numeric(dp_late.loc[cp.index, col])
                both = np.isfinite(cp_late) & np.isfinite(dv)
                if both.any() and np.allclose(cp_late[both], dv[both], rtol=0, atol=1e-9) and (
                    np.isfinite(cp_late) == np.isfinite(dv)
                ).all():
                    suffix = e_suf
                    break
        if suffix is None:
            unmatched.append(code)
            continue
        items.append({
            "cp_early": code, "cp_late": late_code, "ss": ss_code, "label": cp_meta[code]["label"],
            "dp_early": b_early, "dp_late": b_late, "suffix": suffix,
            "dollar": "INFLATION-ADJUSTED DOLLARS" in label and "(DOLLARS)" in label.split("!!")[-1],
        })
    return {"items": items, "unmatched": unmatched, "cp": cp, "dp_late": dp_late, "dp_early": dp_early}


def _cp_data(source: CensusSource, b: Battery, ctx: Dict[str, Any], late: int, early: int) -> Dict[str, Any]:
    key = f"cp_{late}_{early}"
    if key not in ctx:
        ctx[key] = _cp_items(source, b, late, early)
    return ctx[key]


def check_f6_cp_inflation(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    ctx = ctx if ctx is not None else {}
    b = battery
    res = ValidationResult(
        "F6",
        "Comparison Profile (CP03) earlier-period dollar values, which Census publishes in current-period "
        "dollars, vs the earlier release's DP03 value adjusted with inflation.adjust_inflation "
        f"(R-CPI-U-RS, the default), states {b.state_list()}, CP releases "
        + ", ".join(f"{late} (vs {early})" for late, early in b.cp_releases) + ".",
        tolerance="|ours - CP| <= max($1, 0.5% of CP), asserted for every item Census converts with a single "
        "factor (means, aggregates, per-capita); medians reported (see notes)",
    )
    res.census_source += [CPI_GUIDANCE, f"{BASE_URL}/<year>/acs/acs5/cprofile", f"{BASE_URL}/<year>/acs/acs5/profile"]
    failures: List[Dict[str, Any]] = []
    abs_e: List[float] = []
    rel_e: List[float] = []
    n = n_failed = 0
    factors: Dict[str, Any] = {}
    median_misses: List[Dict[str, Any]] = []
    for late, early in b.cp_releases:
        data = _cp_data(source, b, ctx, late, early)
        ours_factor = _inflation.inflation_factor(early, late)
        implied_median: List[float] = []
        implied_all: List[float] = []
        implied_agg: List[float] = []
        identity_bad = 0
        n_items = 0
        for it in data["items"]:
            if not it["dollar"] or it["suffix"] != "E":
                continue
            n_items += 1
            for g in data["cp"].index:
                cp_v = _numeric([data["cp"].at[g, it["cp_early"]]])[0]
                dp_col = it["dp_early"] + "E"
                dp_v = _numeric([data["dp_early"].at[g, dp_col]])[0] if dp_col in data["dp_early"].columns else math.nan
                if not (np.isfinite(cp_v) and np.isfinite(dp_v)) or dp_v == 0:
                    continue
                ours = _inflation.adjust_inflation(dp_v, early, late)
                err = ours - cp_v
                n += 1
                abs_e.append(abs(err))
                rel_e.append(abs(err) / abs(cp_v))
                implied = cp_v / dp_v
                implied_all.append(implied)
                if not re.search(r"MEDIAN", it["label"], re.I):
                    implied_agg.append(implied)
                else:
                    implied_median.append(implied)
                is_median = bool(re.search(r"MEDIAN", it["label"], re.I))
                if abs(err) > max(1.0, 0.005 * abs(cp_v)) + 1e-9:
                    row = {"cp_release": late, "GEOID": g, "item": it["cp_early"], "label": it["label"],
                           "dp_value": dp_v, "ours": round(ours, 2), "published": cp_v,
                           "implied_factor": round(implied, 6)}
                    if is_median:
                        median_misses.append(row)
                    else:
                        n_failed += 1
                        failures.append(row)
                # sanity: CP current-period value == DP current release value
                lv = _numeric([data["cp"].at[g, it["cp_late"]]])[0]
                dl = _numeric([data["dp_late"].at[g, it["dp_late"] + "E"]])[0]
                if np.isfinite(lv) and np.isfinite(dl) and lv != dl:
                    identity_bad += 1
        factors[f"{late} vs {early}"] = {
            "dollar_items": n_items,
            "ours_r_cpi_u_rs_factor": ours_factor,
            "ours_index_values": {str(early): _inflation.get_cpi(early), str(late): _inflation.get_cpi(late)},
            "census_implied_factor_all_items": _distribution(implied_all),
            "census_implied_factor_means_and_per_capita": _distribution(implied_agg),
            "census_implied_factor_medians": _distribution(implied_median),
            "implied_minus_ours_percent": (
                (float(np.median(implied_agg)) / ours_factor - 1) * 100 if implied_agg else None
            ),
            "current_period_cp_ne_dp": identity_bad,
            "unmatched_cp_items": len(data["unmatched"]),
        }
    res.n_compared = n
    res.n_failed = n_failed
    res.max_abs_error = _max(abs_e)
    res.max_rel_error = _max(rel_e)
    res.failures = failures[:MAX_FAILURE_SAMPLES] + [dict(r, asserted=False) for r in median_misses][:10]
    res.details = {"factors": factors, "median_items_outside_tolerance_not_asserted": len(median_misses)}
    res.notes += [
        "Asserted: means, aggregates and per-capita values, which Census converts with a single factor "
        "(implied factor constant to 5 digits across items and states). Median items are compared and "
        "reported but not asserted: in the 2019 Comparison Profile their implied factors scatter "
        "(1.0747-1.0838 against 1.08097 for means), showing that Census re-derived those medians from "
        "inflation-adjusted distributions rather than scaling the published median, which no deflator can "
        "reproduce from published data; in the 2023 CP medians scale with the same constant factor and all "
        "pass. Median misses are listed with asserted=false.",
        "Round 6 acceptance criterion 6: the Census-implied factor (median over mean/aggregate/per-capita "
        "items) is compared with our R-CPI-U-RS factor in details.factors.*.implied_minus_ours_percent.",
    ]
    return res.finish(n_failed == 0 and n > 0)


def check_f7_cp_significance(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    ctx = ctx if ctx is not None else {}
    b = battery
    res = ValidationResult(
        "F7",
        "Comparison Profile significance flags (CP03 '..._SS' variables: '*' = the two periods differ at "
        "the 90% level, empty = not significant) vs stats.z_test on the CP estimates with the Data Profile "
        f"MOEs of each release, states {b.state_list()}, CP releases "
        + ", ".join(f"{late} (vs {early})" for late, early in b.cp_releases) + ".",
        tolerance="agreement on >= 98% of tests that are decidable from the published (rounded) values; "
        "every disagreement listed with its z statistic and its z range under rounding",
    )
    res.census_source += [CP_USER_NOTES, f"{BASE_URL}/<year>/acs/acs5/cprofile", f"{BASE_URL}/<year>/acs/acs5/profile"]
    rows: List[Dict[str, Any]] = []
    per: Dict[str, Any] = {}
    for late, early in b.cp_releases:
        data = _cp_data(source, b, ctx, late, early)
        factor = _inflation.inflation_factor(early, late)
        overlap = acs5_overlap_years(late, early)
        skipped = Counter()
        agree = total = 0
        for it in data["items"]:
            moe_suf = "PM" if it["suffix"] == "PE" else "M"
            for g in data["cp"].index:
                if it["ss"] not in data["cp"].columns:
                    skipped["no SS variable"] += 1
                    continue
                e_late = _numeric([data["cp"].at[g, it["cp_late"]]])[0]
                e_early = _numeric([data["cp"].at[g, it["cp_early"]]])[0]
                m_late_col = it["dp_late"] + moe_suf
                m_early_col = it["dp_early"] + moe_suf
                if m_late_col not in data["dp_late"].columns or m_early_col not in data["dp_early"].columns:
                    skipped["no DP MOE"] += 1
                    continue
                m_late = _numeric([data["dp_late"].at[g, m_late_col]])[0]
                m_early = _numeric([data["dp_early"].at[g, m_early_col]])[0]
                if not all(np.isfinite(v) for v in (e_late, e_early, m_late, m_early)):
                    skipped["estimate or MOE not published"] += 1
                    continue
                if not it["dollar"]:
                    dp_e = _numeric([data["dp_early"].at[g, it["dp_early"] + it["suffix"]]])[0]
                    if dp_e != e_early:
                        if "INFLATION-ADJUSTED DOLLARS" in _norm_label(it["label"]):
                            skipped["income-bracket share re-tabulated by Census after inflation adjustment"] += 1
                        else:
                            skipped["earlier CP value differs from earlier DP value"] += 1
                        continue
                else:
                    m_early = m_early * factor  # constant scaling: MOE(kX) = k MOE(X)
                ss = data["cp"].at[g, it["ss"]]
                census_sig = str(ss).strip() == "*"
                t = z_test(e_late, m_late, e_early, m_early, confidence_level=0.90, overlap_years=overlap)
                lo_z, hi_z = _z_rounding_range(e_late, m_late, e_early, m_early, it["suffix"] == "PE",
                                               factor if it["dollar"] else 1.0)
                decidable = hi_z < 1.645 or lo_z >= 1.645
                total += 1
                same = bool(t["significant"]) == census_sig
                agree += int(same)
                rows.append({
                    "cp_release": late, "GEOID": g, "item": it["cp_late"][:-1], "label": it["label"],
                    "estimate_late": e_late, "moe_late": m_late, "estimate_early": e_early,
                    "moe_early": round(m_early, 3), "z": round(float(t["z_statistic"]), 4),
                    "ours_significant": bool(t["significant"]), "census_significant": census_sig,
                    "agree": same, "z_range_under_rounding": [round(lo_z, 3), round(hi_z, 3)],
                    "decidable": decidable,
                })
        per[f"{late} vs {early}"] = {
            "items": len(data["items"]), "tests": total, "agree": agree,
            "agreement_rate": agree / total if total else None, "overlap_years": overlap,
            "skipped": dict(skipped), "unmatched_cp_items": len(data["unmatched"]),
        }
    for key, rel in per.items():
        sub = [r for r in rows if f"{r['cp_release']} vs" in key]
        dec = [r for r in sub if r["decidable"]]
        rel["decidable_tests"] = len(dec)
        rel["decidable_agree"] = sum(r["agree"] for r in dec)
    total = len(rows)
    decidable = [r for r in rows if r["decidable"]]
    undecidable = [r for r in rows if not r["decidable"]]
    disagree = [r for r in decidable if not r["agree"]]
    rate = (len(decidable) - len(disagree)) / len(decidable) if decidable else math.nan
    raw_rate = sum(r["agree"] for r in rows) / total if total else math.nan
    res.n_compared = len(decidable)
    res.n_failed = len(disagree)
    zs = [abs(r["z"] - 1.645) for r in disagree]
    res.max_abs_error = _max(zs)
    res.failures = sorted(disagree, key=lambda r: abs(r["z"] - 1.645)) + sorted(
        [r for r in undecidable if not r["agree"]], key=lambda r: abs(r["z"] - 1.645))[: max(0, MAX_FAILURE_SAMPLES - len(disagree))]
    res.details = {
        "per_release": per,
        "tests_total": total,
        "decidable_tests": len(decidable),
        "undecidable_tests": len(undecidable),
        "agreement_rate_decidable": rate,
        "agreement_rate_all_tests": raw_rate,
        "agreement_rate_undecidable": (sum(r["agree"] for r in undecidable) / len(undecidable)) if undecidable else None,
        "disagreements_abs_z_minus_1645": _distribution(zs),
        "census_flag_encoding": "'*' = statistically significant at 90%; null/empty = not significant "
        "(variable label 'Statistical Significance!!...', predicateType string)",
    }
    res.notes += [
        "The API's cprofile endpoint publishes estimates (E), annotations (EA) and significance (SS) only, "
        "no MOEs; MOEs come from each release's Data Profile (PM for percent items, M for others). For "
        "dollar items the earlier MOE is scaled by the same R-CPI-U-RS factor as the estimate.",
        "Census tests unrounded estimates and standard errors; we test the published (rounded) values, "
        "so disagreements are expected only for z close to the 1.645 critical value.",
        "A test is 'decidable' when every combination of unrounded values consistent with the published "
        "rounding (estimates and MOEs +/- half a unit; a published percent MOE of 0.1 may be any value in "
        "(0, 0.15)) gives z on the same side of 1.645. Only decidable tests can disagree with Census because "
        "of a calculation error; agreement is asserted on them and reported for all tests. Failures list "
        "decidable disagreements first, then undecidable ones.",
        "max_abs_error for this check is the largest |z - 1.645| among decidable disagreements.",
        "Excluded: earlier-period income-bracket shares (Census re-tabulates households into current-dollar "
        "brackets, so the earlier Data Profile shares and MOEs do not apply) and items without a published "
        "estimate or MOE (e.g. 2010-2014 occupation, '-888888888', not comparable after the occupation "
        "code change).",
        "n_compared counts decidable tests.",
    ]
    return res.finish(len(decidable) > 0 and rate >= 0.98)


def _z_rounding_range(
    e1: float, m1: float, e2: float, m2: float, percent: bool, factor2: float = 1.0
) -> Tuple[float, float]:
    """Range of |z| over unrounded values consistent with the published rounding.

    Percent items are published to 0.1 (half unit 0.05); other items to 1
    (half unit 0.5). ``factor2`` scales the second MOE's rounding (an earlier
    MOE converted to current dollars). A percent MOE published as 0.1 is
    treated as possibly anywhere in (0, 0.15).
    """
    h = 0.05 if percent else 0.5

    def bounds(m: float, k: float) -> Tuple[float, float]:
        lo = 0.0 if (percent and m <= 0.1 + 1e-12) else max(0.0, m - h * k)
        return lo, m + h * k

    l1, u1 = bounds(m1, 1.0)
    l2, u2 = bounds(m2, factor2)
    d = abs(e1 - e2)
    dmin, dmax = max(0.0, d - 2 * h), d + 2 * h
    se_max = math.sqrt(u1**2 + u2**2) / 1.645
    se_min = math.sqrt(l1**2 + l2**2) / 1.645
    z_lo = dmin / se_max if se_max > 0 else math.inf
    z_hi = dmax / se_min if se_min > 0 else math.inf
    return z_lo, z_hi


class Counter(dict):
    def __missing__(self, key: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# F8: sums of counties
# ---------------------------------------------------------------------------


def check_f8_county_sums(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    b = battery
    res = ValidationResult(
        "F8",
        "Sum of all counties (aggregate.aggregate_geographies, approximate MOE) vs the published state "
        f"estimate for additive counts ({', '.join(b.f8_variables)}), states {b.state_list()}, releases "
        f"{', '.join(map(str, b.releases))}.",
        tolerance="estimate exact; MOE ratio (approximate sum MOE / published state MOE) reported, not asserted",
    )
    res.census_source.append(f"{BASE_URL}/<year>/acs/acs5")
    failures: List[Dict[str, Any]] = []
    n = n_failed = 0
    abs_e: List[float] = []
    ratios: Dict[str, List[float]] = {}
    for year in b.releases:
        variables = [v + s for v in b.f8_variables for s in ("E", "M")]
        counties = source.api_frame(year, "acs5", variables, {"for": "county:*", "in": f"state:{b.state_list()}"})
        states = source.api_frame(year, "acs5", variables, {"for": f"state:{b.state_list()}"}).set_index("GEOID")
        for v in variables:
            counties[v] = pd.to_numeric(counties[v], errors="coerce")
        mapping = {g: g[:2] for g in counties["GEOID"]}
        meta, _ = _md.vintage_variables("acs5", year)
        agg = _aggregate.aggregate_geographies(
            counties, mapping, estimate_cols=list(b.f8_variables), method="approximate", metadata=meta
        ).set_index("group_id")
        for st in b.states:
            members = int(agg.at[st, "member_count"])
            expected_members = int((counties["GEOID"].str[:2] == st).sum())
            if members != expected_members:
                n_failed += 1
                failures.append({"year": year, "state": st, "problem": "member_count", "ours": members,
                                 "expected": expected_members})
            for v in b.f8_variables:
                ours = float(agg.at[st, v + "E"])
                pub = _raw_float(states.at[st, v + "E"])
                n += 1
                abs_e.append(abs(ours - pub))
                if ours != pub:
                    n_failed += 1
                    failures.append({"year": year, "state": st, "variable": v, "sum_of_counties": ours,
                                     "state_estimate": pub})
                pm = _raw_float(states.at[st, v + "M"])
                om = float(agg.at[st, v + "M"])
                if pm > 0 and np.isfinite(om):
                    ratios.setdefault(v, []).append(om / pm)
    res.n_compared = n
    res.n_failed = n_failed
    res.max_abs_error = _max(abs_e)
    res.failures = failures[:MAX_FAILURE_SAMPLES]
    res.details = {"moe_ratio_sum_approx_over_state_published": {k: _distribution(v) for k, v in ratios.items()}}
    res.notes.append(
        "B01003_001 is controlled (published MOE '*****') at county and state level, so no MOE ratio is "
        "formed for it. The approximate sum MOE ignores covariance between counties (Handbook 2020 ch. 8), "
        "so the ratio scatters around 1; it is reported, not asserted, as the plan specifies."
    )
    return res.finish(n_failed == 0 and n > 0)


# ---------------------------------------------------------------------------
# F10: Summary File
# ---------------------------------------------------------------------------


def check_f10_summary_file(
    source: CensusSource, battery: Battery = DEFAULT_BATTERY, ctx: Optional[Dict[str, Any]] = None
) -> ValidationResult:
    b = battery
    year = b.sf_release
    res = ValidationResult(
        "F10",
        f"ACS {year - 4}-{year} table-based Summary File cells (bulk delivery on www2.census.gov) vs API "
        f"cells for tables {', '.join(b.sf_tables)}, the nation and states {b.state_list()}.",
        tolerance="exact (numeric equality of every estimate and MOE cell)",
    )
    res.census_source.append(SUMMARY_FILE_DOC)
    res.notes.append(
        "Only the first bytes of each national per-table file are requested (HTTP Range); its rows are "
        "sorted by GEO_ID, so the nation and state rows precede the county, tract and block-group rows. "
        "The table-based Summary File exists from the 2021 releases on (summary_file/2021/table-based-SF/); "
        "summary_file/2019/ holds only data/ (the sequence-based files, one archive per state holding every "
        "table), documentation/ and prototype/ (checked 2026-11-02), so "
        f"{', '.join(str(y) for y in b.releases if y < 2021) or 'no release'} is not compared here."
    )
    geo_ids = ["0100000US"] + [f"0400000US{s}" for s in b.states]
    failures: List[Dict[str, Any]] = []
    n = n_failed = 0
    per: Dict[str, Any] = {}
    for table in b.sf_tables:
        sf = source.summary_file_rows(year, table, geo_ids)
        url = SUMMARY_FILE_URL.format(year=year, table=table.lower())
        res.census_source.append(url)
        api_states = source.api_group(year, "acs5", table, {"for": f"state:{b.state_list()}"}).set_index("GEOID")
        api_us = source.api_group(year, "acs5", table, {"for": "us:1"}).set_index("GEOID")
        t_n = t_bad = 0
        missing_rows = [g for g in geo_ids if g not in sf["rows"]]
        for g in missing_rows:
            failures.append({"table": table, "GEO_ID": g, "problem": "row not found in the fetched byte range"})
        t_bad += len(missing_rows)
        for g, row in sf["rows"].items():
            api = api_us if g == "0100000US" else api_states
            key = "US" if g == "0100000US" else g[-2:]
            for col, val in row.items():
                m = re.match(rf"^{table}_([EM])(\d{{3}})$", col)
                if not m:
                    continue
                api_col = f"{table}_{m.group(2)}{m.group(1)}"
                t_n += 1
                a = _raw_float(api.at[key, api_col]) if api_col in api.columns else math.nan
                s = _raw_float(val)
                if not ((math.isnan(a) and math.isnan(s)) or a == s):
                    t_bad += 1
                    if len(failures) < MAX_FAILURE_SAMPLES:
                        failures.append({"table": table, "GEO_ID": g, "cell": api_col, "summary_file": val,
                                         "api": api.at[key, api_col] if api_col in api.columns else None})
        n += t_n
        n_failed += t_bad
        per[table] = {"cells": t_n, "mismatches": t_bad, "rows": len(sf["rows"]), "byte_range": sf.get("byte_range")}
    res.n_compared = n
    res.n_failed = n_failed
    res.max_abs_error = 0.0 if n_failed == 0 else math.nan
    res.failures = failures
    res.details = {"tables": per}
    return res.finish(n_failed == 0 and n > 0)


# ---------------------------------------------------------------------------
# Battery and reports
# ---------------------------------------------------------------------------

CHECKS: Dict[str, Callable[..., ValidationResult]] = {
    "F1": check_f1_pipeline_integrity,
    "F2": check_f2_profile_proportions,
    "F3": check_f3_replicate_moe,
    "F4": check_f4_replicate_proportions,
    "F5": check_f5_medians,
    "F6": check_f6_cp_inflation,
    "F7": check_f7_cp_significance,
    "F8": check_f8_county_sums,
    "F9": check_f9_continuity,
    "F10": check_f10_summary_file,
}


def parse_checks(checks: Optional[Any]) -> List[str]:
    """Normalize a check selection ('F1,F3', ['f2'], None = all) to ordered IDs."""
    if checks is None or checks == "" or checks == []:
        return list(CHECK_IDS)
    items = checks.split(",") if isinstance(checks, str) else list(checks)
    out: List[str] = []
    for item in items:
        cid = str(item).strip().upper()
        if not cid:
            continue
        if cid not in CHECKS:
            raise ValueError(f"Unknown validation check '{item}'. Choose from {', '.join(CHECK_IDS)}.")
        if cid not in out:
            out.append(cid)
    return [c for c in CHECK_IDS if c in out]


def run_battery(
    checks: Optional[Any] = None,
    live: bool = False,
    fixture_dir: Optional[str] = None,
    client: Any = None,
    record: bool = False,
    battery: Optional[Battery] = None,
    source: Optional[CensusSource] = None,
) -> List[ValidationResult]:
    """Run validation checks and return their results (never raises for a failing check).

    Args:
        checks: IDs such as ``"F2,F4"`` or ``["F1"]``; default every check.
        live: query api.census.gov / www2.census.gov instead of frozen fixtures.
        fixture_dir: fixture directory (default ``tests/fixtures/validation``).
        client: CensusAPIClient for live mode (key from ``CENSUS_API_KEY``).
        record: in live mode, (re)write the fixtures used by offline runs.
        battery: coverage (states, releases, tables); default :data:`DEFAULT_BATTERY`.
    """
    ids = parse_checks(checks)
    b = battery or DEFAULT_BATTERY
    src = source or CensusSource(live=live, fixture_dir=fixture_dir, client=client, record=record)
    ctx: Dict[str, Any] = {}
    results: List[ValidationResult] = []
    try:
        for cid in ids:
            start = time.monotonic()
            try:
                result = CHECKS[cid](src, b, ctx)
            except Exception as exc:  # report, never hide: an error fails the battery
                result = ValidationResult(cid, f"Check {cid}", tolerance="")
                result.status = STATUS_ERROR
                result.passed = False
                result.notes.append(f"{type(exc).__name__}: {src.redact(exc)}")
            result.mode = src.mode
            result.seconds = round(time.monotonic() - start, 2)
            result.census_source = [src.redact(u) for u in dict.fromkeys(result.census_source)]
            results.append(result)
    finally:
        for d in ctx.get("_tempdirs", []):
            shutil.rmtree(d, ignore_errors=True)
    return results


def summarize(results: Sequence[ValidationResult]) -> Dict[str, Any]:
    by_status: Dict[str, List[str]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r.check_id)
    return {
        "checks_run": [r.check_id for r in results],
        "passed": by_status.get(STATUS_PASSED, []),
        "failed": by_status.get(STATUS_FAILED, []),
        "errors": by_status.get(STATUS_ERROR, []),
        "skipped": by_status.get(STATUS_SKIPPED, []),
        "all_passed": all(r.passed is not False for r in results),
    }


# Findings from the 2026-11-02 runs, printed in every markdown report so a reader of
# a failing check sees the diagnosis. Update when a module under test changes.
KNOWN_FINDINGS: Tuple[str, ...] = (
    "**F5 (resolved 2026-11-02, census/medians.py):** when the cumulative count reaches exactly 50% at a "
    "bracket boundary and the following bracket(s) are empty, Census publishes the midpoint of the empty "
    "gap (B25063 2019: Sterling County TX 48431, $850; Stonewall County TX 48433, $600). The interpolation "
    "now returns that midpoint; failures matching the pattern are diagnosed as "
    "failures_explained_by_empty_bracket_gap_midpoint.",
    "**F5 (methodological, criterion revised 2026-11-02):** household income: B19001's $10k-$50k-wide "
    "brackets are much coarser than the $2,500 distribution Census interpolates (Subject Definitions, "
    "'Median Income'), so a per-county 2% band measures bracket coarseness, not calculation error (first "
    "live run, 2019-2023: 84.1% of counties within 2%, 99.8% within the published B19013 MOE). The check "
    "asserts median relative error < 1% and >= 98% of counties within the published MOE; the 2% share is "
    "still reported.",
    "**F9 (informational, census/continuity.py):** the Round 6 continuity check flags real 2019->2023 change "
    "in stable concepts (B19001_015-017 top income brackets in nominal dollars, B17001_015, four B27010 "
    "cells), so `run_batch` blocks those extractions in the default concordance mode. Its thresholds are "
    "documented for adjacent, overlapping releases but are applied to any requested pair.",
    "**F2 (methodological):** owner-occupied (DP04_0046) county MOEs from the Handbook proportion formula "
    "miss the band in 215 of 998 county-years (median ratio 0.88); for every one of them the exact replicate "
    "MOE reproduces the DP MOE within 0.1 point, so the gap is the covariance the approximation ignores. "
    "Age 65+ (12-cell sum) approximate MOEs are ~4x the DP MOE (near-controlled age totals).",
    "**F6:** Census's implied R-CPI-U-RS factor exceeds ours by 0.008% (2018->2023: 1.21729 = 449.3/369.1 "
    "vs ours 449.265/369.1) and 0.010% (2014->2019), consistent with Census using the index rounded to one "
    "decimal or an earlier vintage of the research series. 2019 CP medians are re-derived by Census, not "
    "scaled (see F6 notes).",
)


def _fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "-"
        return f"{v:.{digits}g}"
    return str(v)


def report_payload(results: Sequence[ValidationResult], meta: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return _json_safe({
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        **dict(meta or {}),
        "summary": summarize(results),
        "results": [r.to_dict() for r in results],
    })


def write_json_report(results: Sequence[ValidationResult], path: str, meta: Optional[Mapping[str, Any]] = None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report_payload(results, meta), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def _table(rows: Sequence[Sequence[Any]], header: Sequence[str]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(":---" for _ in header) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    return out


def write_markdown_report(
    results: Sequence[ValidationResult], path: str, meta: Optional[Mapping[str, Any]] = None
) -> str:
    """Human-readable report: summary table, then one section per check."""
    meta = dict(meta or {})
    summ = summarize(results)
    lines = [
        "# Validation against Census-published values",
        "",
        f"Generated: {_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}  ",
        f"Mode: {results[0].mode if results else meta.get('mode', '')}  ",
    ]
    for k in ("package_version", "git_commit", "command"):
        if meta.get(k):
            lines.append(f"{k.replace('_', ' ').capitalize()}: `{meta[k]}`  ")
    lines += [
        "",
        "Each check recomputes a number the Census Bureau computed independently and compares "
        "(PLAN_ROUND_7.md section 8). Tolerances and their reasons are stated per check; a widened "
        "tolerance is a visible decision.",
        "",
        f"**Overall: {'PASS' if summ['all_passed'] else 'FAIL'}** "
        f"(passed {len(summ['passed'])}, failed {len(summ['failed'])}, errors {len(summ['errors'])}, "
        f"skipped {len(summ['skipped'])})",
        "",
    ]
    lines += _table(
        [[r.check_id, r.status.upper(), r.n_compared, r.n_failed, _fmt(r.max_abs_error),
          _fmt(r.max_rel_error), r.tolerance] for r in results],
        ["Check", "Status", "Compared", "Failed", "Max abs error", "Max rel error", "Tolerance"],
    )
    lines += ["", "### Known findings", ""] + [f"- {f}" for f in meta.get("known_findings", KNOWN_FINDINGS)]
    for extra in meta.get("preamble", []):
        lines += ["", extra]
    for r in results:
        lines += ["", f"## {r.check_id}: {r.status.upper()}", "", r.description, "",
                  f"- Tolerance: {r.tolerance}",
                  f"- Compared: {r.n_compared}; failed: {r.n_failed}; max abs error: {_fmt(r.max_abs_error)}; "
                  f"max rel error: {_fmt(r.max_rel_error)}; time: {r.seconds} s"]
        for note in r.notes:
            lines.append(f"- {note}")
        commentary = meta.get("commentary", {}).get(r.check_id)
        if commentary:
            lines += ["", commentary]
        if r.details:
            lines += ["", "<details><summary>Details</summary>", "", "```json",
                      json.dumps(_json_safe(r.details), indent=2, ensure_ascii=False), "```", "", "</details>"]
        if r.failures:
            lines += ["", f"Failures / disagreements (first {min(len(r.failures), MAX_FAILURE_SAMPLES)}):", ""]
            keys = list(dict.fromkeys(k for f in r.failures[:MAX_FAILURE_SAMPLES] for k in f))
            lines += _table([[_fmt(f.get(k), 8) if isinstance(f.get(k), float) else f.get(k, "")
                              for k in keys] for f in r.failures[:MAX_FAILURE_SAMPLES]], keys)
        if r.census_source:
            lines += ["", "Sources:", ""] + [f"- {u}" for u in r.census_source[:20]]
            if len(r.census_source) > 20:
                lines.append(f"- ... and {len(r.census_source) - 20} more (see the JSON report)")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def run_validation(
    checks: Optional[Any] = None,
    live: bool = False,
    output_dir: Optional[str] = None,
    fixture_dir: Optional[str] = None,
    record: bool = False,
    client: Any = None,
    write_reports: bool = True,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the battery, write ``validation_report.md``/``.json``, return a JSON-safe summary.

    This is the function the CLI ``validate`` subcommand and the MCP
    ``run_validation`` tool wrap. ``output_dir`` defaults to ``docs/audits``.
    """
    results = run_battery(checks=checks, live=live, fixture_dir=fixture_dir, client=client, record=record)
    payload = report_payload(results, {"mode": results[0].mode if results else "", **dict(meta or {})})
    if write_reports:
        out = output_dir or DEFAULT_REPORT_DIR
        md_path = write_markdown_report(results, os.path.join(out, REPORT_BASENAME + ".md"), meta)
        json_path = write_json_report(results, os.path.join(out, REPORT_BASENAME + ".json"),
                                      {"mode": payload["mode"], **dict(meta or {})})
        payload["report_paths"] = {"markdown": md_path, "json": json_path}
    payload["success"] = payload["summary"]["all_passed"]
    return payload
