# SPDX-License-Identifier: AGPL-3.0-or-later
"""Download per-vintage ACS 5-year, 1-year and 2020 Census metadata into metadata/ (offline reference archive).

For each dataset and each vintage listed in the Census API catalog, saves
``variables.json`` and ``groups.json`` gzipped, and records source URL,
retrieval time, byte size and SHA-256 (of the raw, uncompressed response) in
``metadata/manifest.json``.

Usage:
    python scripts/fetch_metadata_archive.py            # fetch missing files
    python scripts/fetch_metadata_archive.py --refresh  # re-download and report changes
    python scripts/fetch_metadata_archive.py --datasets dec/pl,dec/dhc  # only these datasets

Published vintages are expected to be immutable; --refresh reports any file
whose checksum changed instead of silently replacing history.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(ROOT, "metadata")
MANIFEST = os.path.join(ARCHIVE, "manifest.json")
CATALOG_URL = "https://api.census.gov/data.json"
BASE = "https://api.census.gov/data"

# dataset key used in this repo -> API path
DATASETS: Dict[str, str] = {
    "acs5": "acs/acs5",
    "profile": "acs/acs5/profile",
    "subject": "acs/acs5/subject",
    "cprofile": "acs/acs5/cprofile",
    # ACS 1-year releases (2005 onward; no 2020 release in the API: Census
    # published only experimental 2020 1-year tables, outside the API).
    "acs1": "acs/acs1",
    "acs1/profile": "acs/acs1/profile",
    "acs1/subject": "acs/acs1/subject",
    "acs1/cprofile": "acs/acs1/cprofile",
    # 2020 Decennial Census: P.L. 94-171 and DHC (2020 only; see VINTAGE_FILTER)
    "dec/pl": "dec/pl",
    "dec/dhc": "dec/dhc",
}
# Datasets archived for some vintages only (the catalog also lists 2000/2010 dec/pl).
VINTAGE_FILTER: Dict[str, List[int]] = {"dec/pl": [2020], "dec/dhc": [2020]}
FILES = ("variables.json", "groups.json")


def discover_vintages(session: requests.Session) -> Dict[str, List[int]]:
    catalog = session.get(CATALOG_URL, timeout=120).json()
    paths = {v: k for k, v in DATASETS.items()}
    found: Dict[str, List[int]] = {k: [] for k in DATASETS}
    for entry in catalog.get("dataset", []):
        path = "/".join(entry.get("c_dataset", []))
        year = entry.get("c_vintage")
        if path in paths and year:
            found[paths[path]].append(int(year))
    return {
        k: sorted(y for y in set(v) if k not in VINTAGE_FILTER or y in VINTAGE_FILTER[k])
        for k, v in found.items()
    }


def load_manifest() -> Dict:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {"description": "", "files": {}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="re-download existing files")
    parser.add_argument("--datasets", help="comma-separated dataset keys to fetch (default: all)")
    args = parser.parse_args()
    only = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
    unknown = [d for d in only or [] if d not in DATASETS]
    if unknown:
        parser.error(f"unknown dataset(s) {unknown}; choose from {sorted(DATASETS)}")

    session = requests.Session()
    manifest = load_manifest()
    manifest["description"] = (
        "Per-vintage Census API metadata for ACS 5-year and 1-year datasets and the 2020 "
        "Decennial Census (dec/pl, dec/dhc). Paths are "
        "<dataset>/<year>/<file>.gz; sha256 and bytes refer to the raw "
        "(uncompressed) API response."
    )
    manifest["catalog_url"] = CATALOG_URL
    files = manifest.setdefault("files", {})
    vintages = discover_vintages(session)
    if only is not None:
        vintages = {k: v for k, v in vintages.items() if k in only}
        manifest["vintages"] = {**manifest.get("vintages", {}), **vintages}
    else:
        manifest["vintages"] = vintages
    changed: List[str] = []

    for dataset, years in vintages.items():
        for year in years:
            for name in FILES:
                rel = f"{dataset}/{year}/{name}.gz"
                dest = os.path.join(ARCHIVE, dataset, str(year), f"{name}.gz")
                if os.path.exists(dest) and not args.refresh:
                    continue
                url = f"{BASE}/{year}/{DATASETS[dataset]}/{name}"
                resp = session.get(url, timeout=300)
                if resp.status_code != 200:
                    print(f"SKIP {rel}: HTTP {resp.status_code}", file=sys.stderr)
                    continue
                raw = resp.content
                json.loads(raw)  # must be valid JSON before it enters the archive
                digest = hashlib.sha256(raw).hexdigest()
                old = files.get(rel, {}).get("sha256")
                if old and old != digest:
                    changed.append(rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with gzip.GzipFile(dest, "wb", compresslevel=9, mtime=0) as gz:
                    gz.write(raw)
                files[rel] = {
                    "url": url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "bytes": len(raw),
                    "sha256": digest,
                }
                print(f"saved {rel} ({len(raw) // 1024} KB)")
                time.sleep(0.2)

    manifest["files"] = dict(sorted(files.items()))
    os.makedirs(ARCHIVE, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    if changed:
        print("WARNING: published metadata changed for:", *changed, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
