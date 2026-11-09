# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACS errata notes and user notes by release and table (PLAN_ROUND_9 workstream F).

The Census Bureau lists errata notes (``.../technical-documentation/errata.html``)
and user notes (``.../technical-documentation/user-notes.html``) for ACS
releases. ``scripts/fetch_acs_errata.py`` walks both listings, fetches every
note page and stores one record per note in ``data/acs_errata.json`` (with a
sidecar ``data/acs_errata.sidecar.json``: source pages, retrieval date,
SHA-256).

Records hold only what the pages state: ``title``, ``date`` and ``excerpt``
are copied from the note; ``tables``, ``release_years`` and ``products`` are
the table IDs, ACS data years and products (1-year, 5-year, ...) named in the
note's title and text, extracted by the patterns below. A note that names no
table is a general note (``tables == []``); one that names no product is
``products == []`` (unspecified). Nothing is inferred beyond the text, so read
the note (``url``) before acting on it.

Entry points: :func:`load_errata`, :func:`errata_for` (lookup used by
planning), :func:`check_urls` (validation: every cited URL resolves; live
only), and the parsers used by the fetch script.
"""

import datetime as _dt
import hashlib
import html as _html
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from census.geo import get_resource_path

ERRATA_LISTING_URL = "https://www.census.gov/programs-surveys/acs/technical-documentation/errata.html"
USER_NOTES_LISTING_URL = "https://www.census.gov/programs-surveys/acs/technical-documentation/user-notes.html"
CENSUS_ORIGIN = "https://www.census.gov"
ERRATA_PATH = os.path.join("data", "acs_errata.json")
SIDECAR_PATH = os.path.join("data", "acs_errata.sidecar.json")

# Table IDs: detailed (B/C + 5 digits, race iteration A-I, Puerto Rico 'PR'),
# subject (S####, optional iteration letter), profiles (DP02-05, CP02-05),
# supplemental (K20####), ranking (R####) and geographic comparison (GCT####).
_TABLE_RE = re.compile(
    r"\b(?:[BC]\d{5}[A-I]?(?:PR)?|S\d{4}(?:PR)?|DP0[2-5](?:PR)?|CP0[2-5](?:PR)?|K20\d{4}|R\d{4}|GCT\d{4})\b"
)
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December")
_DATE_RE = re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}},\s+(?:19|20)\d{{2}}\b")
_MONTH_YEAR_RE = re.compile(rf"\b(?:{_MONTHS})\s+(?:19|20)\d{{2}}\b")
# "2015-2019 ACS 5-year" names ONE 5-year release (ending 2019).
_PERIOD_RE = re.compile(r"\b(20\d{2})\s*[-–]\s*(20\d{2})\s+(?:ACS\s+)?5-?\s*[Yy]ear")
_RANGE_RE = re.compile(r"\b(20\d{2})\s*(?:through|to|thru|[-–])\s*(20\d{2})\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_PRODUCT_RES = {
    "1-year": re.compile(r"\b1-?\s*year\b|\bone-year\b", re.I),
    "3-year": re.compile(r"\b3-?\s*year\b|\bthree-year\b", re.I),
    "5-year": re.compile(r"\b5-?\s*year\b|\bfive-year\b", re.I),
    "1-year supplemental": re.compile(r"\bsupplemental estimates?\b", re.I),
    "PUMS": re.compile(r"\bPUMS\b|\bPublic Use Microdata\b", re.I),
}
FIRST_ACS_YEAR = 2005


# ---------------------------------------------------------------------------
# Parsing (pure functions; the fetch script does the HTTP)
# ---------------------------------------------------------------------------


def _text(fragment: str) -> str:
    fragment = re.sub(r"(?s)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", fragment)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_listing(page_html: str, kind: str) -> List[Dict[str, str]]:
    """Note links on one listing page: [{url, title, date, label}] in page order.

    kind: 'errata' or 'user_note' (which link paths count as notes).
    """
    sub = "errata" if kind == "errata" else "user-notes"
    pattern = re.compile(
        r'<a[^>]+href="(?P<href>[^"]*/technical-documentation/' + sub + r'/[^"]+\.html)"[^>]*>(?P<body>.*?)</a>',
        re.S,
    )
    out, seen = [], set()
    for m in pattern.finditer(page_html):
        href = m.group("href")
        url = href if href.startswith("http") else CENSUS_ORIGIN + href
        if url in seen:
            continue
        seen.add(url)
        body = m.group("body")
        title = re.search(r'cmp-list__item-title[^>]*>(.*?)</div>', body, re.S)
        date = re.search(r'cmp-list__item-date[^>]*>(.*?)</div>', body, re.S)
        label = re.search(r'cmp-list__item-description[^>]*>(.*?)</div>', body, re.S)
        out.append({
            "url": url,
            "title": _text(title.group(1)) if title else _text(body),
            "date": _text(date.group(1)) if date else "",
            "label": _text(label.group(1)) if label else "",
        })
    return out


def _meta(page_html: str, name: str) -> str:
    m = re.search(r'<meta[^>]+name="' + re.escape(name) + r'"[^>]*content="([^"]*)"', page_html)
    return _html.unescape(m.group(1)).strip() if m else ""


def note_body(page_html: str) -> str:
    """Plain text of a note: after the section navigation, before 'Page Last Revised'."""
    text = _text(page_html)
    title = _meta(page_html, "DC.title")
    end = text.find("Page Last Revised")
    if end < 0:
        end = len(text)
    # The title appears in <title>, the nav, and the H1; the body follows the last
    # occurrence before the footer.
    start = text.rfind(title, 0, end) + len(title) if title and title in text[:end] else 0
    return text[start:end].strip()


def extract_tables(text: str) -> List[str]:
    return sorted(set(_TABLE_RE.findall(text)))


def extract_release_years(text: str, max_year: Optional[int] = None) -> List[int]:
    """ACS data years named in a note (publication dates removed first).

    'YYYY-YYYY ACS 5-year' names the release ending in the second year;
    'YYYY through/to YYYY' names every year in the range.
    """
    max_year = max_year or _dt.date.today().year
    text = _MONTH_YEAR_RE.sub(" ", _DATE_RE.sub(" ", text))
    years = set()
    for m in _PERIOD_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if b - a == 4:
            years.add(b)
            text = text.replace(m.group(0), " ")
    for m in _RANGE_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if FIRST_ACS_YEAR <= a < b <= max_year and b - a <= 20:
            years.update(range(a, b + 1))
    for m in _YEAR_RE.finditer(text):
        y = int(m.group(1))
        if FIRST_ACS_YEAR <= y <= max_year:
            years.add(y)
    return sorted(years)


def extract_products(text: str) -> List[str]:
    return [name for name, rx in _PRODUCT_RES.items() if rx.search(text)]


def parse_note(page_html: str, url: str, kind: str, retrieved_at: str,
               listing: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One record from a note page. Fields are copied or pattern-extracted only."""
    listing = listing or {}
    title = _meta(page_html, "DC.title") or listing.get("title", "")
    label = _meta(page_html, "description") or listing.get("label", "")
    body = note_body(page_html)
    scan = f"{title} {body}"
    created = _meta(page_html, "DC.date.created")[:10]
    # The title names what the note is about; the text also names related
    # tables (e.g. a table to use instead) and later releases (e.g. where the
    # error was fixed). Use the title when it names tables/years, else the text.
    title_tables, title_years = extract_tables(title), extract_release_years(title)
    number = re.search(r"(\d+)", label) if kind == "errata" else None
    return {
        "id": (f"errata-{int(number.group(1))}" if number else
               f"{kind}-" + os.path.splitext(url.rstrip("/").rsplit("/", 1)[-1])[0]),
        "kind": kind,
        "label": label,
        "title": title,
        "date": listing.get("date") or created,
        "date_iso": created,
        "url": url,
        "tables": title_tables or extract_tables(body),
        "tables_source": "title" if title_tables else "text",
        "tables_mentioned": extract_tables(scan),
        "release_years": title_years or extract_release_years(body),
        "release_years_source": "title" if title_years else "text",
        "products": extract_products(scan),
        "excerpt": body[:600],
        "retrieved_at": retrieved_at,
    }


# ---------------------------------------------------------------------------
# Dataset and lookup
# ---------------------------------------------------------------------------


def errata_path() -> str:
    return get_resource_path(ERRATA_PATH)


def load_errata(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Records from data/acs_errata.json ([] when the file is absent)."""
    path = path or errata_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return list(doc.get("records", doc) if isinstance(doc, dict) else doc)


def _base_table(code: str) -> str:
    """'B17001_002E' -> 'B17001'; 'b17001a' -> 'B17001A'."""
    return str(code).strip().upper().split("_", 1)[0]


def errata_for(
    table: str,
    years: Optional[Iterable[int]] = None,
    product: Optional[str] = "5-year",
    include_general: bool = False,
    records: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Errata and user notes that name ``table`` (and any of ``years``).

    A note matches when it names the table (exactly, or the table's race/PR
    iteration base, e.g. a note on B17001 matches B17001A), names at least one
    requested year (a note naming no year matches every year), and names
    ``product`` or no product at all. ``include_general`` also returns notes
    that name no table but match the years and product (e.g. a question
    change). Results are sorted by date, newest first.
    """
    recs = load_errata() if records is None else records
    want = _base_table(table)
    stem = re.match(r"^([BC]\d{5}|S\d{4}|DP0\d|CP0\d)", want)
    candidates = {want, stem.group(1)} if stem else {want}
    year_set = {int(y) for y in years} if years is not None else None
    out = []
    for rec in recs:
        tables = set(rec.get("tables") or [])
        if tables:
            if not (tables & candidates):
                continue
        elif not include_general:
            continue
        rec_years = set(rec.get("release_years") or [])
        if year_set is not None and rec_years and not (rec_years & year_set):
            continue
        prods = rec.get("products") or []
        if product and prods and product not in prods:
            continue
        out.append(dict(rec))
    return sorted(out, key=lambda r: r.get("date_iso") or "", reverse=True)


def errata_summary(table: str, years: Optional[Iterable[int]] = None) -> List[Dict[str, Any]]:
    """Compact form for plans and manifests: id, title, url, release_years."""
    return [
        {k: r.get(k) for k in ("id", "kind", "title", "url", "release_years", "tables")}
        for r in errata_for(table, years)
    ]


def check_urls(
    records: Optional[Sequence[Dict[str, Any]]] = None,
    live: bool = False,
    get: Optional[Callable[[str], int]] = None,
    delay: float = 1.0,
) -> Dict[str, Any]:
    """Validation helper: every cited note URL resolves (HTTP 200). Live only.

    Offline (``live=False``) returns ``{"skipped": True}`` with the URL count;
    ``get`` (url -> status code) can be injected for tests.
    """
    recs = load_errata() if records is None else records
    urls = sorted({r["url"] for r in recs if r.get("url")})
    if not live:
        return {"skipped": True, "reason": "live check only", "n_urls": len(urls), "failures": []}
    if get is None:
        import time

        import requests

        session = requests.Session()
        session.headers["User-Agent"] = "census-research-engine/errata-check (research toolkit)"

        def get(url: str) -> int:  # type: ignore[no-redef]
            time.sleep(delay)
            try:
                return session.get(url, timeout=30, allow_redirects=True).status_code
            except requests.RequestException:
                return 0

    failures = []
    for url in urls:
        status = get(url)
        if status != 200:
            failures.append({"url": url, "status": status})
    return {"skipped": False, "n_urls": len(urls), "n_failed": len(failures), "failures": failures,
            "passed": not failures}


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
