# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build data/geo_crosswalks/ from Census Bureau tract relationship files.

Values are never typed by hand. Downloads are treated as untrusted data and
only parsed. Each output gets a sidecar JSON with source URLs, retrieval time,
HTTP Last-Modified, and SHA-256 of the raw download and of the extract.

1. 2020 Census Tract to 2010 Census Tract Relationship File (national,
   pipe-delimited, UTF-8; record layout "2020 Comparability Relationship File
   Record Layouts"):
       https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/tab20_tract20_tract10_natl.txt
   -> data/geo_crosswalks/tract_2010_2020.csv.gz (+ .json sidecar)
   Columns kept: both GEOIDs, land and water area of the intersection, and
   land and water area of each whole tract (square meters).

2. Connecticut recode. ACS releases from 2022 on tabulate Connecticut by its
   nine planning regions (county-equivalents 09110-09190) instead of the eight
   former counties, so a Connecticut 2020-Census tract carries a different
   GEOID in 2020/2021 releases than in 2022+ releases, although tract shapes are
   unchanged. The Census Bureau publishes no direct 2020-to-2022 tract file;
   this script derives one from the "2022 County Subdivision to 2022 Census
   Tract for Connecticut" file:
       https://www2.census.gov/geo/docs/maps-data/data/rel2022/acs22_cousub22_tract22_st09.txt
   matching on the 6-digit tract code (unique statewide for land tracts),
   checking land areas agree, and failing loudly if any tract is unmatched or
   ambiguous. Water-only tracts (99xxxx) are listed as unmapped.
   -> data/geo_crosswalks/ct_tract_2020_2022.csv (+ .json sidecar)

Usage:
    python scripts/fetch_tract_relationships.py
    python scripts/fetch_tract_relationships.py --national-file PATH --ct-file PATH
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "geo_crosswalks")

NATIONAL_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/tab20_tract20_tract10_natl.txt"
)
NATIONAL_DOCS = [
    "https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.2020.html",
    "https://www.census.gov/programs-surveys/geography/technical-documentation/records-layout/2020-comp-record-layout.html",
    "https://www2.census.gov/geo/pdfs/maps-data/data/rel2020/tract/explanation_tab20_tract20_tract10.pdf",
]
CT_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2022/acs22_cousub22_tract22_st09.txt"
CT_DOCS = [
    "https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.2022.html",
    "https://www.census.gov/programs-surveys/geography/technical-documentation/records-layout/2022-connecticut-record-layout.html",
    "https://www.census.gov/programs-surveys/acs/technical-documentation/user-notes/2023-01.html",
]
# Maximum relative land-area difference accepted for a Connecticut tract code match.
LAND_TOLERANCE = 0.10
# Matches whose land area differs by more than this are listed in the sidecar.
LAND_REPORT = 0.01
USER_AGENT = "acs-batch-downloader/fetch_tract_relationships (research toolkit)"

# Output column -> source column in the national file.
NATIONAL_COLUMNS: List[Tuple[str, str]] = [
    ("geoid_2010", "GEOID_TRACT_10"),
    ("geoid_2020", "GEOID_TRACT_20"),
    ("arealand_part", "AREALAND_PART"),
    ("areawater_part", "AREAWATER_PART"),
    ("arealand_2010", "AREALAND_TRACT_10"),
    ("areawater_2010", "AREAWATER_TRACT_10"),
    ("arealand_2020", "AREALAND_TRACT_20"),
    ("areawater_2020", "AREAWATER_TRACT_20"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, local: Optional[str]) -> Tuple[bytes, Dict[str, Optional[str]]]:
    if local:
        with open(local, "rb") as f:
            return f.read(), {"retrieved_from": "local copy of " + url, "last_modified": None}
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=300)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "html" in ctype.lower():
        raise RuntimeError(f"{url} returned {ctype}; possibly blocked. Download it manually.")
    return resp.content, {"retrieved_from": url, "last_modified": resp.headers.get("Last-Modified")}


def parse_pipe(raw: bytes) -> List[Dict[str, str]]:
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text), delimiter="|"))


def _geoid(value: str, field: str) -> str:
    value = (value or "").strip()
    if not (len(value) == 11 and value.isdigit()):
        raise ValueError(f"unexpected {field} value {value!r}")
    return value


def _area(value: str, field: str) -> int:
    value = (value or "").strip()
    if not value.isdigit():
        raise ValueError(f"unexpected {field} value {value!r}")
    return int(value)


def write_sidecar(path: str, sidecar: Dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(sidecar, f, indent=2)
        f.write("\n")


def build_national(raw: bytes, origin: Dict[str, Optional[str]]) -> List[Dict[str, object]]:
    rows = parse_pipe(raw)
    if not rows:
        raise RuntimeError("national file is empty")
    missing = [src for _, src in NATIONAL_COLUMNS if src not in rows[0]]
    if missing:
        raise RuntimeError(f"national file lacks columns {missing}; record layout changed?")
    out: List[Dict[str, object]] = []
    for row in rows:
        rec: Dict[str, object] = {}
        for name, src in NATIONAL_COLUMNS:
            rec[name] = _geoid(row[src], src) if name.startswith("geoid") else _area(row[src], src)
        out.append(rec)
    out.sort(key=lambda r: (r["geoid_2010"], r["geoid_2020"]))

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    names = [n for n, _ in NATIONAL_COLUMNS]
    writer.writerow(names)
    for rec in out:
        writer.writerow([rec[n] for n in names])
    csv_bytes = buf.getvalue().encode("utf-8")

    os.makedirs(OUT_DIR, exist_ok=True)
    gz_path = os.path.join(OUT_DIR, "tract_2010_2020.csv.gz")
    with open(gz_path, "wb") as fh:
        # filename="" and mtime=0 keep the gzip header byte-stable across runs.
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
            gz.write(csv_bytes)

    sidecar = {
        "name": "2020 Census Tract to 2010 Census Tract Relationship File (national), compact extract",
        "publisher": "U.S. Census Bureau, Geography Division",
        "source_url": NATIONAL_URL,
        "documentation": NATIONAL_DOCS,
        "retrieved_at": _now(),
        "retrieved_from": origin["retrieved_from"],
        "source_last_modified": origin["last_modified"],
        "source_bytes": len(raw),
        "source_sha256": _sha256(raw),
        "source_format": "pipe-delimited UTF-8 text, one row per 2020-tract x 2010-tract intersection",
        "extract_file": "tract_2010_2020.csv.gz",
        "extract_columns": {n: s for n, s in NATIONAL_COLUMNS},
        "area_units": "square meters",
        "rows": len(out),
        "tracts_2010": len({r["geoid_2010"] for r in out}),
        "tracts_2020": len({r["geoid_2020"] for r in out}),
        # Checksum of the decompressed CSV: independent of zlib version and OS.
        "csv_sha256": _sha256(csv_bytes),
        "notes": [
            "2020 Connecticut tract GEOIDs use the former 8 counties (09001-09015); see ct_tract_2020_2022.csv for the planning-region GEOIDs used by ACS 2022+ releases.",
            "Rows are sorted by geoid_2010, geoid_2020; the gzip header has mtime=0 and no filename.",
        ],
    }
    write_sidecar(os.path.join(OUT_DIR, "tract_2010_2020.json"), sidecar)
    print(f"wrote {gz_path}: {len(out)} rows, {os.path.getsize(gz_path)} bytes")
    return out


def build_ct(raw: bytes, origin: Dict[str, Optional[str]], national: List[Dict[str, object]]) -> None:
    rows = parse_pipe(raw)
    need = ["GEOID_TRACT_22", "AREALAND_TRACT_22", "AREAWATER_TRACT_22"]
    if not rows or any(c not in rows[0] for c in need):
        raise RuntimeError(f"Connecticut file lacks columns {need}; record layout changed?")
    t22: Dict[str, Tuple[int, int]] = {}
    for row in rows:
        g = _geoid(row["GEOID_TRACT_22"], "GEOID_TRACT_22")
        t22[g] = (_area(row["AREALAND_TRACT_22"], "AREALAND_TRACT_22"),
                  _area(row["AREAWATER_TRACT_22"], "AREAWATER_TRACT_22"))
    t20: Dict[str, Tuple[int, int]] = {}
    for rec in national:
        g = str(rec["geoid_2020"])
        if g.startswith("09"):
            t20[g] = (int(rec["arealand_2020"]), int(rec["areawater_2020"]))  # type: ignore[arg-type]

    by_code_20: Dict[str, List[str]] = {}
    by_code_22: Dict[str, List[str]] = {}
    for g in t20:
        by_code_20.setdefault(g[5:], []).append(g)
    for g in t22:
        by_code_22.setdefault(g[5:], []).append(g)
    if set(by_code_20) != set(by_code_22):
        raise RuntimeError(
            "Connecticut tract codes differ between 2020 and 2022: "
            f"{sorted(set(by_code_20) ^ set(by_code_22))[:10]}"
        )
    pairs: List[Tuple[str, str]] = []
    water_only: List[str] = []
    problems: List[str] = []
    max_rel_land = 0.0
    area_changed: List[Dict[str, object]] = []
    for code in sorted(by_code_20):
        olds, news = by_code_20[code], by_code_22[code]
        if code.startswith("99"):
            # Water-only tracts (9900xx) recur once per county / planning region,
            # have no land and no ACS population, and have no published 2020->2022
            # intersection, so they are listed as unmapped rather than guessed.
            if any(t20[g][0] for g in olds) or any(t22[g][0] for g in news):
                problems.append(f"tract code {code} has land area")
            water_only.extend(olds)
            continue
        if len(olds) != 1 or len(news) != 1:
            problems.append(f"tract code {code}: 2020 {olds} / 2022 {news}")
            continue
        a, b = t20[olds[0]][0], t22[news[0]][0]
        rel = abs(a - b) / max(a, b, 1)
        max_rel_land = max(max_rel_land, rel)
        if rel > LAND_TOLERANCE:
            problems.append(f"{olds[0]} -> {news[0]}: land area {a} vs {b}")
            continue
        if rel > LAND_REPORT:
            area_changed.append({"geoid_2020": olds[0], "geoid_2022": news[0],
                                 "arealand_2020": a, "arealand_2022": b})
        pairs.append((olds[0], news[0]))
    if problems:
        raise RuntimeError(f"Connecticut 2020->2022 tract match failed: {problems[:10]}")

    path = os.path.join(OUT_DIR, "ct_tract_2020_2022.csv")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["geoid_2020", "geoid_2022"])
    writer.writerows(pairs)
    csv_bytes = buf.getvalue().encode("utf-8")
    with open(path, "wb") as f:
        f.write(csv_bytes)
    sidecar = {
        "name": "Connecticut census tract GEOID recode: 2020 county-based -> 2022 planning-region-based",
        "publisher": "Derived by this toolkit from U.S. Census Bureau relationship files",
        "source_url": CT_URL,
        "also_uses": NATIONAL_URL,
        "documentation": CT_DOCS,
        "retrieved_at": _now(),
        "retrieved_from": origin["retrieved_from"],
        "source_last_modified": origin["last_modified"],
        "source_bytes": len(raw),
        "source_sha256": _sha256(raw),
        "method": "Match on the 6-digit tract code, which is unique statewide for every "
        "Connecticut tract with land in both vintages; land areas must agree within "
        f"{LAND_TOLERANCE:.0%} (2022 TIGER areas differ slightly from 2020 after "
        "boundary corrections; tract codes and delineations were otherwise kept).",
        "max_relative_land_area_difference": round(max_rel_land, 6),
        f"land_area_differs_over_{LAND_REPORT:.0%}": area_changed,
        "rows": len(pairs),
        "unmapped_water_only_2020": sorted(water_only),
        "unchanged_tract_code": sum(1 for a, b in pairs if a[5:] == b[5:]),
        "csv_sha256": _sha256(csv_bytes),
    }
    write_sidecar(os.path.join(OUT_DIR, "ct_tract_2020_2022.json"), sidecar)
    print(f"wrote {path}: {len(pairs)} rows")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--national-file", help="use a local copy of tab20_tract20_tract10_natl.txt")
    ap.add_argument("--ct-file", help="use a local copy of acs22_cousub22_tract22_st09.txt")
    args = ap.parse_args(argv)
    raw, origin = download(NATIONAL_URL, args.national_file)
    national = build_national(raw, origin)
    raw_ct, origin_ct = download(CT_URL, args.ct_file)
    build_ct(raw_ct, origin_ct, national)
    return 0


if __name__ == "__main__":
    sys.exit(main())
