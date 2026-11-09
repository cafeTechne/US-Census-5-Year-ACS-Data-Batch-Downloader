# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build data/acs_errata.json from the Census ACS errata and user-notes pages.

Walks every page of the two listings (tab 'all'), fetches each note page and
parses it with census.errata.parse_note. Writes the records and a sidecar
(data/acs_errata.sidecar.json) with the listing URLs, the retrieval date, the
record count and the SHA-256 of the data file. Requests are spaced by --delay
as a courtesy, on top of census.http's pacing and retries.

Usage: python scripts/fetch_acs_errata.py [--delay 2] [--html-cache DIR]
--html-cache keeps the raw pages (for re-parsing without re-fetching).
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from census import errata  # noqa: E402
from census.http import polite_get  # noqa: E402

LISTINGS = {
    "errata": errata.ERRATA_LISTING_URL,
    "user_note": errata.USER_NOTES_LISTING_URL,
}
USER_AGENT = "census-research-engine/errata (research toolkit; python-requests)"
MAX_PAGES = 40


class Fetcher:
    def __init__(self, delay: float, cache_dir: str = ""):
        self.delay = delay
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get(self, url: str) -> str:
        if self.cache_dir:
            name = hashlib.sha256(url.encode()).hexdigest()[:24] + ".html"
            path = os.path.join(self.cache_dir, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return f.read()
        time.sleep(self.delay)
        resp = polite_get(url, user_agent=USER_AGENT, error=requests.RequestException, session=self.session,
                          timeout=60)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        text = resp.text
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text


def _list_id(page_html: str) -> str:
    m = re.search(r"changeTab\(\s*'all',\s*'([^']+)'", page_html)
    return m.group(1).encode().decode("unicode_escape") if m else ""


def walk_listing(fetch: Fetcher, kind: str, base_url: str):
    first = fetch.get(base_url)
    list_id = _list_id(first)
    items, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = f"{base_url}?{list_id}:tab=all&{list_id}:page={page}" if list_id else base_url
        found = [i for i in errata.parse_listing(fetch.get(url), kind) if i["url"] not in seen]
        if not found:
            break
        for item in found:
            seen.add(item["url"])
            items.append(item)
        if not list_id:
            break
    return items, list_id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--html-cache", default="")
    args = parser.parse_args(argv)
    fetch = Fetcher(args.delay, args.html_cache)
    today = dt.date.today().isoformat()
    records, sources = [], []
    for kind, base in LISTINGS.items():
        items, list_id = walk_listing(fetch, kind, base)
        sources.append({"listing": base, "list_id": list_id, "notes_listed": len(items)})
        print(f"{kind}: {len(items)} notes listed", file=sys.stderr)
        for item in items:
            try:
                page = fetch.get(item["url"])
            except requests.RequestException as exc:
                print(f"  skipped {item['url']}: {exc}", file=sys.stderr)
                sources[-1].setdefault("unfetched", []).append(item["url"])
                continue
            records.append(errata.parse_note(page, item["url"], kind, today, item))
    records.sort(key=lambda r: (r["kind"], r.get("date_iso") or ""), reverse=True)
    out_path = os.path.join(ROOT, errata.ERRATA_PATH)
    doc = {
        "description": "ACS errata notes and user notes (census.errata). Titles, dates and excerpts are "
                       "copied from the pages; tables, release_years and products are the IDs, years "
                       "and products the note's title and text name.",
        "records": records,
    }
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    sidecar = {
        "kind": "acs_errata",
        "retrieved": today,
        "sources": sources,
        "records": len(records),
        "errata_notes": sum(r["kind"] == "errata" for r in records),
        "user_notes": sum(r["kind"] == "user_note" for r in records),
        "sha256": errata.sha256_file(out_path),
        "generator": "scripts/fetch_acs_errata.py",
    }
    with open(os.path.join(ROOT, errata.SIDECAR_PATH), "w", encoding="utf-8", newline="\n") as f:
        json.dump(sidecar, f, indent=2)
        f.write("\n")
    print(f"Wrote {len(records)} records to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
