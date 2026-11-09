# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polite file downloads from the census.gov file servers (www2.census.gov, www.census.gov).

Every module that downloads a file from census.gov (VRE tables, boundary
files, PUMS, block relationship files, validation sources, errata pages)
goes through :func:`polite_get`, which

* identifies the client honestly with the caller's own User-Agent string
  (``census-research-engine/<module> ...``); it never poses as a browser;
* keeps a process-wide minimum interval between requests to the same host
  (``CENSUS_WWW2_MIN_INTERVAL`` seconds, default 1);
* retries HTTP 429/500/502/503/504 and connection errors or timeouts with
  exponential backoff and jitter (``CENSUS_WWW2_ATTEMPTS`` attempts, default
  4; delay ``CENSUS_WWW2_BACKOFF_BASE`` x 2^k seconds, default base 2, capped
  at 60, times a random factor in [0.5, 1]), honouring a ``Retry-After``
  header (capped at 300 s);
* recognises the census.gov firewall page ("Request Rejected ... Your support
  ID is: N", served with HTTP 200), retries it at most once after
  ``CENSUS_WWW2_REJECTED_RETRY_DELAY`` seconds (default 30), and then raises
  the caller's error class with the support ID, the URL and, when the caller
  passes it, the exact cache path where a manually downloaded copy is picked up.

The firewall page is not (only) a rate limit: on 2026-11-09 www2 rejected one
specific file (``replicate_estimates/2019/data/5-year/140/B01003_26.csv.zip``)
on every request, whatever the pacing or client, while every other file in
the same directory downloaded normally. Retrying in a loop does not help, so
the helper never loops; the message tells the user to fetch the URL in a
browser and place it at the cache path, where :func:`adopt_manual_file`
records it (SHA-256, ``source: "manual download"``) on first read.
"""

import email.utils
import hashlib
import json
import os
import random
import re
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Type
from urllib.parse import urlsplit

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
RETRY_AFTER_CAP = 300.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_REJECTED_RETRY_DELAY = 30.0
MANUAL_SOURCE = "manual download"

ENV_MIN_INTERVAL = "CENSUS_WWW2_MIN_INTERVAL"
ENV_ATTEMPTS = "CENSUS_WWW2_ATTEMPTS"
ENV_BACKOFF_BASE = "CENSUS_WWW2_BACKOFF_BASE"
ENV_REJECTED_RETRY_DELAY = "CENSUS_WWW2_REJECTED_RETRY_DELAY"

_REJECTED_MARKER = b"Request Rejected"
_SUPPORT_ID = re.compile(rb"support\s+ID\s+is:?\s*(?:<[^>]*>\s*)*([0-9A-Za-z-]+)", re.IGNORECASE)

# Indirections so tests can drive time and jitter without sleeping.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic
_jitter: Callable[[], float] = lambda: random.uniform(0.5, 1.0)  # noqa: E731

_pace_lock = threading.Lock()
_last_request: Dict[str, float] = {}


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ[name]))
    except (KeyError, ValueError):
        return default


def min_interval() -> float:
    return _env_float(ENV_MIN_INTERVAL, DEFAULT_MIN_INTERVAL)


def attempts() -> int:
    return max(1, int(_env_float(ENV_ATTEMPTS, DEFAULT_ATTEMPTS)))


def backoff_delay(attempt: int) -> float:
    """Delay before retry number ``attempt`` (0-based): base x 2^attempt, capped, times jitter."""
    base = _env_float(ENV_BACKOFF_BASE, DEFAULT_BACKOFF_BASE)
    return min(BACKOFF_CAP, base * 2**attempt) * _jitter()


def retry_after_seconds(value: Any) -> Optional[float]:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP date), capped; None if absent/invalid."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        secs = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        secs = when.timestamp() - time.time()
    return min(RETRY_AFTER_CAP, max(0.0, secs))


def rejection_support_id(body: bytes) -> Optional[str]:
    """None if ``body`` is not the census.gov firewall page; else its support ID ('' if none shown)."""
    head = body[:4000]
    if _REJECTED_MARKER not in head:
        return None
    m = _SUPPORT_ID.search(head)
    return m.group(1).decode("ascii", "replace") if m else ""


def _pace(host: str) -> None:
    gap = min_interval()
    with _pace_lock:
        last = _last_request.get(host)
        if last is not None and gap > 0:
            wait = gap - (_monotonic() - last)
            if wait > 0:
                _sleep(wait)
        _last_request[host] = _monotonic()


def _raise(error: Type[Exception], message: str, **attrs: Any) -> None:
    exc = error(message)
    for k, v in attrs.items():
        setattr(exc, k, v)
    raise exc


def rejected_message(url: str, support_id: str, cache_path: Optional[str], retried_after: Optional[float]) -> str:
    sid = f"support ID {support_id}" if support_id else "no support ID shown"
    again = (f", also on one retry {retried_after:g} s later" if retried_after is not None else "")
    where = (f"save it at exactly this path:\n  {cache_path}\nThe toolkit then uses that file and records its "
             f"SHA-256 with source '{MANUAL_SOURCE}'."
             if cache_path else "use the downloaded file instead of the URL.")
    return (f"census.gov rejected the request for {url} with its firewall 'Request Rejected' page ({sid}){again}. "
            "This rejection can be persistent and specific to one URL (other files on the same server may "
            "download normally), so retrying will not help and the toolkit does not retry further. "
            f"Download the file in a web browser from {url} and {where}")


def polite_get(
    url: str,
    *,
    user_agent: str,
    error: Type[Exception] = RuntimeError,
    session: Any = None,
    timeout: float = 300.0,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    stream: bool = False,
    cache_path: Optional[str] = None,
) -> Any:
    """GET ``url`` politely (see module docstring) and return the response.

    Non-retryable statuses (and a retryable one after the last attempt) are
    returned for the caller to judge, so callers keep their own 404/HTTP
    messages. Raises ``error`` after the last connection failure, and on a
    firewall rejection that persists through its single retry; the exception
    carries ``url``, ``support_id``, ``cache_path`` and ``rejected`` attributes.
    ``session`` is anything with a requests-style ``get``; default ``requests``.
    """
    import requests  # local: keeps importers usable without network deps

    getter = session if session is not None else requests
    kwargs: Dict[str, Any] = {"headers": {**(headers or {}), "User-Agent": user_agent}, "timeout": timeout}
    if params is not None:
        kwargs["params"] = params
    if stream:
        kwargs["stream"] = True
    host = urlsplit(url).netloc.lower()
    n = attempts()
    attempt = 0
    rejected_retry: Optional[float] = None
    while True:
        _pace(host)
        try:
            resp = getter.get(url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt + 1 >= n:
                _raise(error, f"Download failed for {url} after {n} attempt(s): {exc}",
                       url=url, support_id=None, cache_path=cache_path, rejected=False)
            _sleep(backoff_delay(attempt))
            attempt += 1
            continue
        except requests.RequestException as exc:
            _raise(error, f"Download failed for {url}: {exc}", url=url, support_id=None,
                   cache_path=cache_path, rejected=False)
        status = getattr(resp, "status_code", 200)
        if status in RETRY_STATUSES and attempt + 1 < n:
            hdrs = getattr(resp, "headers", None) or {}
            wait = retry_after_seconds(hdrs.get("Retry-After"))
            _sleep(wait if wait is not None else backoff_delay(attempt))
            attempt += 1
            continue
        if not stream or "html" in str((getattr(resp, "headers", None) or {}).get("Content-Type", "")).lower():
            sid = rejection_support_id(resp.content or b"")
            if sid is not None:
                if rejected_retry is None:
                    rejected_retry = _env_float(ENV_REJECTED_RETRY_DELAY, DEFAULT_REJECTED_RETRY_DELAY)
                    _sleep(rejected_retry)
                    continue
                _raise(error, rejected_message(url, sid, cache_path, rejected_retry), url=url, support_id=sid,
                       cache_path=cache_path, rejected=True)
        return resp


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def adopt_manual_file(
    path: str,
    url: str,
    sidecar: Optional[str] = None,
    validate: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Record a file the user placed at a cache ``path`` by hand; return its new sidecar metadata.

    Returns None when ``path`` does not exist or already has a sidecar (the
    caller's normal cache check applies). ``validate(path)`` may raise to
    refuse a file that is not what the URL serves (for example a saved HTML
    page); the sidecar is then not written. The sidecar holds the source URL,
    SHA-256, size and ``source: "manual download"``, so provenance records
    say the file was not fetched by the toolkit.
    """
    side = sidecar or path + ".json"
    if not os.path.isfile(path) or os.path.exists(side):
        return None
    if validate is not None:
        validate(path)
    meta = {
        "source_url": url,
        "sha256": _sha256_file(path),
        "size_bytes": os.path.getsize(path),
        "content_type": "",
        "source": MANUAL_SOURCE,
        "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(side, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta
