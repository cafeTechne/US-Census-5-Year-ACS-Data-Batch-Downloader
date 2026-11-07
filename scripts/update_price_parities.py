# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build data/price_parities/ from BEA Regional Price Parities downloads.

Values are never typed by hand. Sources (BEA Regional Economic Accounts,
public bulk downloads, no API key; listed at
https://apps.bea.gov/regional/downloadzip.htm):

    https://apps.bea.gov/regional/zip/SARPP.zip  RPPs by state (SARPP) + implicit regional
                                                 price deflators (SAIRPD) + real PI (SARPI)
    https://apps.bea.gov/regional/zip/PARPP.zip  RPPs by state metropolitan / nonmetropolitan portion
    https://apps.bea.gov/regional/zip/MARPP.zip  RPPs by metropolitan statistical area (+ U.S. nonmetro)

Outputs:
    data/price_parities/rpp.csv              geo_fips, geo_name, geo_type, year, rpp_all_items,
                                             rpp_goods, rpp_housing, rpp_utilities, rpp_other
    data/price_parities/pce_price_index.csv  year, value  (U.S. implicit regional price deflator
                                             = national PCE price index, 2017=100, same vintage)
    data/price_parities/rpp.json             sidecar: sources, BEA release note, SHA-256 of
                                             each raw zip and of the CSVs
    data/price_parities/source/*.zip         the raw downloads

With --sainc-zip PATH (BEA SAINC.zip, state annual personal income) the
script also writes tests/fixtures/rpp/bea_real_personal_income.csv: nominal
personal income (SAINC1 line 1) next to BEA's published real personal income
(SARPI line 1) for every state in 2008, used by tests/test_rpp.py.

Usage:
    python scripts/update_price_parities.py                     # download
    python scripts/update_price_parities.py --zip-dir DIR       # use local SARPP/PARPP/MARPP zips
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "price_parities")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "rpp", "bea_real_personal_income.csv")
BASE_URL = "https://apps.bea.gov/regional/zip/"
ZIPS = ("SARPP", "PARPP", "MARPP")
LINES = {"1": "rpp_all_items", "2": "rpp_goods", "3": "rpp_housing", "4": "rpp_utilities", "5": "rpp_other"}
# 2008 is the one year SAINC1 (revised 2009-2025 on 2026-09-30) shares with the RPP vintage
# (2026-02-19); later years mix vintages. All states are needed for BEA's additivity step.
REFERENCE_YEARS = ("2008",)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(zf: zipfile.ZipFile, prefix: str) -> Tuple[List[str], List[List[str]], List[str]]:
    name = next(n for n in zf.namelist() if n.startswith(prefix + "_") and n.endswith(".csv"))
    text = zf.read(name).decode("latin-1")
    rows = list(csv.reader(io.StringIO(text)))
    header = [h.strip() for h in rows[0]]
    data = [r for r in rows[1:] if len(r) == len(header)]
    trailer = [" ".join(r).strip() for r in rows[1:] if len(r) != len(header) and any(c.strip() for c in r)]
    return header, data, trailer


def _geo_type(table: str, fips: str, name: str) -> str:
    if fips == "00000":
        return "us"
    if table == "MARPP":
        return "us_nonmetro" if fips == "00999" else "msa"
    if table == "PARPP":
        return "state_nonmetro" if fips.endswith("999") else "state_metro"
    return "state"


def _num(s: str) -> Optional[float]:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None  # (NA), (NM)


def parse(blobs: Dict[str, bytes]) -> Tuple[List[Dict], Dict[int, float], Dict[str, List[str]]]:
    records: Dict[Tuple[str, int], Dict] = {}
    notes: Dict[str, List[str]] = {}
    pce: Dict[int, float] = {}
    for table in ZIPS:
        zf = zipfile.ZipFile(io.BytesIO(blobs[table]))
        header, data, trailer = _rows(zf, table)
        notes[table] = trailer
        years = [int(h) for h in header[8:]]
        for r in data:
            fips = r[0].strip().strip('"').strip()
            line = r[4].strip()
            if line not in LINES:
                continue
            gtype = _geo_type(table, fips, r[1])
            if table != "SARPP" and gtype == "us":
                continue  # the U.S. row is identical across files
            for y, v in zip(years, r[8:]):
                rec = records.setdefault((fips, y), {"geo_fips": fips, "geo_name": r[1].strip().rstrip(" *").strip(),
                                                     "geo_type": gtype, "year": y})
                rec[LINES[line]] = _num(v)
        if table == "SARPP":
            h2, d2, _ = _rows(zf, "SAIRPD")
            for r in d2:
                if r[0].strip().strip('"') == "00000" and r[4].strip() == "1":
                    pce = {int(y): float(v) for y, v in zip(h2[8:], r[8:]) if _num(v) is not None}
    out = [rec for rec in records.values() if rec.get("rpp_all_items") is not None]
    out.sort(key=lambda d: (d["geo_type"], d["geo_fips"], d["year"]))
    return out, pce, notes


def _load(zip_dir: Optional[str]) -> Dict[str, bytes]:
    blobs = {}
    for t in ZIPS:
        if zip_dir:
            with open(os.path.join(zip_dir, f"{t}.zip"), "rb") as f:
                blobs[t] = f.read()
        else:
            import requests
            resp = requests.get(BASE_URL + f"{t}.zip", timeout=120,
                                headers={"User-Agent": os.getenv("BEA_USER_AGENT", "census-acs-toolkit")})
            resp.raise_for_status()
            blobs[t] = resp.content
    return blobs


def _write_csv(path: str, cols: List[str], rows: List[Dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else (f"{r[c]:.3f}" if isinstance(r[c], float) else r[c])) for c in cols})
    data = buf.getvalue().encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    return _sha(data)


def write_reference_fixture(sainc_zip: str, sarpp_blob: bytes) -> None:
    zf = zipfile.ZipFile(sainc_zip)
    name = next(n for n in zf.namelist() if n.startswith("SAINC1__") and n.endswith(".csv"))
    rows = list(csv.reader(io.StringIO(zf.read(name).decode("latin-1"))))
    hdr = [h.strip() for h in rows[0]]
    nominal = {r[0].strip().strip('"'): dict(zip(hdr[8:], r[8:])) for r in rows[1:]
               if len(r) == len(hdr) and r[4].strip() == "1"}
    sainc_note = [" ".join(r).strip() for r in rows[1:] if len(r) != len(hdr) and "updated" in " ".join(r).lower()]
    h2, d2, _ = _rows(zipfile.ZipFile(io.BytesIO(sarpp_blob)), "SARPI")
    real = {r[0].strip().strip('"'): (r[1].strip(), dict(zip(h2[8:], r[8:]))) for r in d2 if r[4].strip() == "1"}
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", newline="", encoding="utf-8") as f:
        f.write(f"# nominal: BEA SAINC1 line 1 ({'; '.join(sainc_note)}); real: BEA SARPI line 1 "
                "(SARPP.zip). Millions of dollars / millions of constant 2017 dollars.\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["geo_fips", "geo_name", "year", "personal_income_nominal", "real_personal_income_published"])
        for g in sorted(real):
            for y in REFERENCE_YEARS:
                w.writerow([g, real[g][0], y, nominal[g][y].strip(), real[g][1][y].strip()])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip-dir", help="directory with SARPP.zip, PARPP.zip, MARPP.zip (skip download)")
    ap.add_argument("--sainc-zip", help="BEA SAINC.zip, to (re)write the real-personal-income test fixture")
    args = ap.parse_args(argv)
    blobs = _load(args.zip_dir)
    records, pce, notes = parse(blobs)
    os.makedirs(os.path.join(OUT_DIR, "source"), exist_ok=True)
    raw = {}
    for t, b in blobs.items():
        p = os.path.join(OUT_DIR, "source", f"{t}.zip")
        with open(p, "wb") as f:
            f.write(b)
        raw[t] = {"url": BASE_URL + f"{t}.zip", "sha256": _sha(b), "file": f"data/price_parities/source/{t}.zip",
                  "bea_notes": notes[t]}
    cols = ["geo_fips", "geo_name", "geo_type", "year"] + list(LINES.values())
    rpp_sha = _write_csv(os.path.join(OUT_DIR, "rpp.csv"), cols, records)
    pce_sha = _write_csv(os.path.join(OUT_DIR, "pce_price_index.csv"), ["year", "value"],
                         [{"year": y, "value": v} for y, v in sorted(pce.items())])
    years = sorted({r["year"] for r in records})
    side = {
        "name": "BEA Regional Price Parities",
        "description": "Regional price parities (U.S. = 100) by state, state metropolitan and nonmetropolitan "
                       "portion, and metropolitan statistical area; all items and four components",
        "publisher": "U.S. Bureau of Economic Analysis, Regional Economic Accounts",
        "documentation": "https://www.bea.gov/data/prices-inflation/regional-price-parities-state-and-metro-area",
        "sources": raw,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "line codes 1-5 of SARPP, PARPP and MARPP copied as published (3 decimals); "
                  "pce_price_index = SAIRPD line 1 for the United States (RPP = 100, so the U.S. implicit "
                  "regional price deflator equals the national PCE price index, 2017=100)",
        "msa_delineation": "OMB bulletin 23-01 (July 21, 2023)",
        "nonmetro_note": "BEA nonmetropolitan portions include micropolitan counties",
        "years": [years[0], years[-1]],
        "counts": {gt: len({r["geo_fips"] for r in records if r["geo_type"] == gt})
                   for gt in sorted({r["geo_type"] for r in records})},
        "csv_sha256": {"rpp.csv": rpp_sha, "pce_price_index.csv": pce_sha},
        "estimation_error_note": "RPPs are estimates (CPI price quotes, ACS rents, hedonic models) with their own "
                                 "error, which BEA does not publish as standard errors; it is not in ACS MOEs.",
    }
    with open(os.path.join(OUT_DIR, "rpp.json"), "w", encoding="utf-8") as f:
        json.dump(side, f, indent=2)
        f.write("\n")
    if args.sainc_zip:
        write_reference_fixture(args.sainc_zip, blobs["SARPP"])
    print(f"wrote {len(records)} RPP rows ({years[0]}-{years[-1]}) to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
