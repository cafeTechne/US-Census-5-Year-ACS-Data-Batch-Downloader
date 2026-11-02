# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build data/price_indexes/*.csv from Bureau of Labor Statistics sources.

Values are never typed by hand. Each CSV gets a sidecar JSON with the source,
retrieval time, method, and SHA-256 of the raw download.

CPI-U (series CUUR0000SA0) and C-CPI-U (chained CPI, series SUUR0000SA0) come
from the BLS Public Data API v1, which needs no key. Annual averages are the
mean of the 12 monthly indexes, rounded to 3 decimals (the BLS definition of an
annual average).

Years with an unpublished month (2025: October, never published because of the
2025 lapse in appropriations) are not written to the CSV. When BLS nonetheless
published an annual figure for such a year, it is recorded in the sidecar's
"provisional" section, which census.inflation uses only when the caller passes
allow_provisional=True.

R-CPI-U-RS (all items) is published only as an Excel file:
    https://www.bls.gov/cpi/research-series/r-cpi-u-rs-allitems.xlsx
BLS blocks anonymous scripted downloads. Either:
    1. download that file in a browser and pass --r-cpi-u-rs-xlsx PATH, or
    2. set BLS_USER_AGENT to an identifying User-Agent that includes a contact
       email, which is what BLS asks automated clients to send.

Usage:
    python scripts/update_price_indexes.py                         # CPI-U only
    python scripts/update_price_indexes.py --skip-cpi-u \
        --r-cpi-u-rs-xlsx data/price_indexes/source/r-cpi-u-rs-allitems.xlsx
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from statistics import mean
from typing import Dict, List, Tuple

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "price_indexes")
BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
CPI_U_SERIES = "CUUR0000SA0"
C_CPI_U_SERIES = "SUUR0000SA0"
R_CPI_U_RS_URL = "https://www.bls.gov/cpi/research-series/r-cpi-u-rs-allitems.xlsx"
START_YEAR = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def provisional_section(values: Dict[int, float], missing: List[str], basis: str) -> Dict[str, Dict]:
    """Sidecar entries for years with an unpublished month but a published annual figure."""
    out: Dict[str, Dict] = {}
    for year, value in sorted(values.items()):
        gaps = sorted(m for m in missing if m.startswith(f"{year}-"))
        if gaps:
            out[str(year)] = {
                "value": round(value, 3),
                "months_published": 12 - len(gaps),
                "unpublished_months": gaps,
                "basis": basis,
            }
    return out


def write_index(name: str, values: Dict[int, float], sidecar: Dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{name}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["year", "value"])
        for year in sorted(values):
            writer.writerow([year, f"{values[year]:.3f}"])
    with open(path, "rb") as f:
        sidecar["csv_sha256"] = hashlib.sha256(f.read()).hexdigest()
    sidecar["years"] = [min(values), max(values)]
    with open(os.path.join(OUT_DIR, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(sidecar, f, indent=2)
        f.write("\n")
    print(f"wrote {path}: {min(values)}-{max(values)}")


def fetch_bls_series(series_id: str) -> Dict:
    """Annual averages from monthly values of a BLS CPI series (10-year API windows).

    Returns a dict with "annual" (complete years only), "missing" (unpublished
    months), "published_annual" (BLS's own M13 annual figures, which can exist
    for incomplete years), "preliminary" (years with a preliminary-footnoted
    month), and "digests" (SHA-256 of each raw API response).
    """
    this_year = datetime.now().year
    monthly: Dict[int, Dict[str, float]] = {}
    published_annual: Dict[int, float] = {}
    missing: List[str] = []
    preliminary: set = set()
    digests: List[str] = []
    for start in range(START_YEAR, this_year + 1, 10):
        end = min(start + 9, this_year)
        resp = requests.post(
            BLS_API,
            json={"seriesid": [series_id], "startyear": str(start), "endyear": str(end)},
            timeout=60,
        )
        resp.raise_for_status()
        digests.append(hashlib.sha256(resp.content).hexdigest())
        payload = resp.json()
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS API error: {payload.get('message')}")
        for row in payload["Results"]["series"][0]["data"]:
            period = row["period"]
            if not period.startswith("M"):
                continue
            try:
                value = float(row["value"])
            except ValueError:  # "-" marks a month BLS did not publish
                if period != "M13":
                    missing.append(f"{row['year']}-{period}")
                continue
            if period == "M13":
                published_annual[int(row["year"])] = value
                continue
            monthly.setdefault(int(row["year"]), {})[period] = value
            if any("preliminary" in str(fn.get("text", "")).lower() for fn in row.get("footnotes", []) if fn):
                preliminary.add(int(row["year"]))
    annual = {
        year: round(mean(months.values()), 3)
        for year, months in monthly.items()
        if len(months) == 12
    }
    return {
        "annual": annual,
        "missing": sorted(missing),
        "published_annual": published_annual,
        "preliminary": sorted(preliminary),
        "digests": digests,
    }


def fetch_cpi_u() -> Tuple[Dict[int, float], List[str]]:
    """Backward-compatible wrapper: (complete-year CPI-U annual averages, digests)."""
    result = fetch_bls_series(CPI_U_SERIES)
    return result["annual"], result["digests"]


def parse_r_cpi_u_rs(raw: bytes, with_incomplete: bool = False):
    """Read the annual average column from the BLS R-CPI-U-RS workbook.

    Years with an unpublished month are excluded, even when the workbook shows
    an AVG for them (BLS averaged 11 months for 2025), to match the CPI-U rule.
    Returns (values, unpublished months as "YYYY-MNN"), plus the workbook's
    AVG for incomplete years when with_incomplete=True.
    """
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    header_idx = None
    year_col = avg_col = None
    values: Dict[int, float] = {}
    missing: List[str] = []
    incomplete: Dict[int, float] = {}
    for idx, row in enumerate(ws.iter_rows(values_only=True)):
        cells = [str(c).strip().upper() if c is not None else "" for c in row]
        if header_idx is None:
            if "YEAR" in cells and any(c.startswith("AVG") or c == "ANNUAL" for c in cells):
                header_idx = idx
                year_col = cells.index("YEAR")
                avg_col = next(i for i, c in enumerate(cells) if c.startswith("AVG") or c == "ANNUAL")
            continue
        try:
            year = int(float(row[year_col]))
            value = float(row[avg_col])
        except (TypeError, ValueError):
            continue
        gaps = [
            f"{year}-M{m:02d}"
            for m, cell in enumerate(row[year_col + 1 : avg_col], start=1)
            if not isinstance(cell, (int, float))
        ]
        if gaps:
            missing.extend(gaps)
            incomplete[year] = value
            continue
        values[year] = round(value, 3)
    if not values:
        raise RuntimeError("Could not find YEAR and AVG columns in the R-CPI-U-RS workbook.")
    if with_incomplete:
        return values, missing, incomplete
    return values, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--r-cpi-u-rs-xlsx", help="path to a manually downloaded r-cpi-u-rs-allitems.xlsx")
    parser.add_argument("--skip-cpi-u", action="store_true", help="do not refresh CPI-U or C-CPI-U from the BLS API")
    args = parser.parse_args()

    if not args.skip_cpi_u:
        api_series = [
            (
                "cpi_u",
                CPI_U_SERIES,
                "CPI-U",
                "CPI for All Urban Consumers, U.S. city average, all items, not seasonally adjusted",
                "1982-84=100",
                None,
            ),
            (
                "c_cpi_u",
                C_CPI_U_SERIES,
                "C-CPI-U",
                "Chained CPI for All Urban Consumers, U.S. city average, all items, not seasonally adjusted "
                "(used by the Census Bureau for CPS ASEC income from 2000 onward)",
                "December 1999=100",
                "C-CPI-U values are preliminary when first published and are revised for about 10-12 "
                "months; years listed in preliminary_years contain preliminary months and will change.",
            ),
        ]
        for name, series_id, display, description, base, extra_note in api_series:
            result = fetch_bls_series(series_id)
            sidecar = {
                "name": display,
                "series_id": series_id,
                "description": description,
                "base_period": base,
                "source": BLS_API + series_id,
                "method": "annual average = mean of 12 monthly indexes, rounded to 3 decimals",
                "retrieved_at": _now(),
                "raw_sha256": result["digests"],
                "unpublished_months": result["missing"],
                "note": "Years with an unpublished month have no annual average in the CSV; see provisional.",
                "provisional": provisional_section(
                    result["published_annual"], result["missing"], "BLS published annual average (period M13)"
                ),
                "preliminary_years": result["preliminary"],
            }
            if extra_note:
                sidecar["revision_note"] = extra_note
            write_index(name, {y: v for y, v in result["annual"].items() if y >= START_YEAR}, sidecar)

    raw = None
    source = R_CPI_U_RS_URL
    if args.r_cpi_u_rs_xlsx:
        with open(args.r_cpi_u_rs_xlsx, "rb") as f:
            raw = f.read()
        source = f"{R_CPI_U_RS_URL} (downloaded manually)"
    elif os.getenv("BLS_USER_AGENT"):
        resp = requests.get(R_CPI_U_RS_URL, headers={"User-Agent": os.environ["BLS_USER_AGENT"]}, timeout=60)
        resp.raise_for_status()
        raw = resp.content
    else:
        print(
            "R-CPI-U-RS not updated: pass --r-cpi-u-rs-xlsx PATH or set BLS_USER_AGENT "
            "(see this script's docstring).",
            file=sys.stderr,
        )
    if raw is not None:
        r_values, r_missing, r_incomplete = parse_r_cpi_u_rs(raw, with_incomplete=True)
        write_index(
            "r_cpi_u_rs",
            {y: v for y, v in r_values.items() if y >= START_YEAR},
            {
                "name": "R-CPI-U-RS",
                "description": "Retroactive CPI-U research series, all items (used by the Census Bureau for ACS inflation adjustment)",
                "base_period": "December 1977=100",
                "source": source,
                "method": "annual average column (AVG) from the BLS workbook, rounded to 3 decimals",
                "retrieved_at": _now(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_file": "data/price_indexes/source/r-cpi-u-rs-allitems.xlsx",
                "unpublished_months": r_missing,
                "note": "Years with an unpublished month are excluded from the CSV even if the workbook shows an AVG; see provisional.",
                "provisional": provisional_section(r_incomplete, r_missing, "BLS workbook AVG column"),
            },
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
