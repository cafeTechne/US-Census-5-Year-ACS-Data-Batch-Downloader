# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build data/price_indexes/*.csv from Bureau of Labor Statistics sources.

Values are never typed by hand. Each CSV gets a sidecar JSON with the source,
retrieval time, method, and SHA-256 of the raw download.

CPI-U (series CUUR0000SA0) comes from the BLS Public Data API v1, which needs
no key. Annual averages are the mean of the 12 monthly indexes, rounded to 3
decimals (the BLS definition of an annual average).

R-CPI-U-RS (all items) is published only as an Excel file:
    https://www.bls.gov/cpi/research-series/r-cpi-u-rs-allitems.xlsx
BLS blocks anonymous scripted downloads. Either:
    1. download that file in a browser and pass --r-cpi-u-rs-xlsx PATH, or
    2. set BLS_USER_AGENT to an identifying User-Agent that includes a contact
       email, which is what BLS asks automated clients to send.

Usage:
    python scripts/update_price_indexes.py                         # CPI-U only
    python scripts/update_price_indexes.py --r-cpi-u-rs-xlsx r.xlsx
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
R_CPI_U_RS_URL = "https://www.bls.gov/cpi/research-series/r-cpi-u-rs-allitems.xlsx"
START_YEAR = 2000
MISSING_MONTHS: List[str] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_index(name: str, values: Dict[int, float], sidecar: Dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{name}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["year", "value"])
        for year in sorted(values):
            writer.writerow([year, f"{values[year]:.3f}"])
    with open(path, "rb") as f:
        sidecar["csv_sha256"] = hashlib.sha256(f.read()).hexdigest()
    sidecar["years"] = [min(values), max(values)]
    with open(os.path.join(OUT_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
        f.write("\n")
    print(f"wrote {path}: {min(values)}-{max(values)}")


def fetch_cpi_u() -> Tuple[Dict[int, float], List[str]]:
    """Annual averages from monthly CPI-U via the BLS API (10-year windows)."""
    this_year = datetime.now().year
    monthly: Dict[int, Dict[str, float]] = {}
    digests: List[str] = []
    for start in range(START_YEAR, this_year + 1, 10):
        end = min(start + 9, this_year)
        resp = requests.post(
            BLS_API,
            json={"seriesid": [CPI_U_SERIES], "startyear": str(start), "endyear": str(end)},
            timeout=60,
        )
        resp.raise_for_status()
        digests.append(hashlib.sha256(resp.content).hexdigest())
        payload = resp.json()
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS API error: {payload.get('message')}")
        for row in payload["Results"]["series"][0]["data"]:
            period = row["period"]
            if period.startswith("M") and period != "M13":
                try:
                    value = float(row["value"])
                except ValueError:  # "-" marks a month BLS did not publish
                    MISSING_MONTHS.append(f"{row['year']}-{period}")
                    continue
                monthly.setdefault(int(row["year"]), {})[period] = value
    annual = {
        year: round(mean(months.values()), 3)
        for year, months in monthly.items()
        if len(months) == 12
    }
    return annual, digests


def parse_r_cpi_u_rs(raw: bytes) -> Dict[int, float]:
    """Read the annual average column from the BLS R-CPI-U-RS workbook."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    header_idx = None
    year_col = avg_col = None
    values: Dict[int, float] = {}
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
        values[year] = round(value, 3)
    if not values:
        raise RuntimeError("Could not find YEAR and AVG columns in the R-CPI-U-RS workbook.")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--r-cpi-u-rs-xlsx", help="path to a manually downloaded r-cpi-u-rs-allitems.xlsx")
    parser.add_argument("--skip-cpi-u", action="store_true")
    args = parser.parse_args()

    if not args.skip_cpi_u:
        values, digests = fetch_cpi_u()
        write_index(
            "cpi_u",
            {y: v for y, v in values.items() if y >= START_YEAR},
            {
                "name": "CPI-U",
                "series_id": CPI_U_SERIES,
                "description": "CPI for All Urban Consumers, U.S. city average, all items, not seasonally adjusted",
                "base_period": "1982-84=100",
                "source": BLS_API + CPI_U_SERIES,
                "method": "annual average = mean of 12 monthly indexes, rounded to 3 decimals",
                "retrieved_at": _now(),
                "raw_sha256": digests,
                "unpublished_months": sorted(MISSING_MONTHS),
                "note": "Years with an unpublished month have no computed annual average here.",
            },
        )

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
        write_index(
            "r_cpi_u_rs",
            parse_r_cpi_u_rs(raw),
            {
                "name": "R-CPI-U-RS",
                "description": "Retroactive CPI-U research series, all items (used by the Census Bureau for ACS inflation adjustment)",
                "base_period": "December 1977=100",
                "source": source,
                "method": "annual average column (AVG) from the BLS workbook",
                "retrieved_at": _now(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
