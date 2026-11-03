# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture real tract estimates and MOEs for the simulation CV profile (Round 9 G3).

Writes ``tests/fixtures/cv_profile/tract_moes_<year>.csv`` (one row per tract,
raw API values) and a ``.json`` sidecar with source URLs (no API key), the
retrieval date and the SHA-256. ``census.simulation.derive_cv_profile`` reads
it; ``CV_PROFILES['tract']`` stores the derived numbers.

Usage: python scripts/fetch_tract_cv_fixture.py [--year 2023]
Needs CENSUS_API_KEY in the environment or .env (it is never written to disk).
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlencode

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "tests", "fixtures", "cv_profile")

# Detailed-table estimate/MOE pairs: a count, two medians and the poverty universe.
VARIABLES = [
    "B17001_001E", "B17001_001M",  # population for whom poverty status is determined
    "B17001_002E", "B17001_002M",  # income below poverty level
    "B19013_001E", "B19013_001M",  # median household income
    "B25064_001E", "B25064_001M",  # median gross rent
]
# (state, county): urban, suburban and rural counties in four regions.
COUNTIES = [
    ("48", "453"),  # Travis County, TX
    ("06", "019"),  # Fresno County, CA
    ("36", "005"),  # Bronx County, NY
    ("17", "019"),  # Champaign County, IL
    ("56", "021"),  # Laramie County, WY
    ("13", "121"),  # Fulton County, GA
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", type=int, default=2023)
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass
    key = os.getenv("CENSUS_API_KEY", "")
    base = f"https://api.census.gov/data/{args.year}/acs/acs5"
    frames, urls = [], []
    for state, county in COUNTIES:
        params = {"get": "NAME," + ",".join(VARIABLES), "for": "tract:*", "in": f"state:{state} county:{county}"}
        urls.append(f"{base}?{urlencode(params)}")
        query = dict(params, key=key) if key else params
        resp = requests.get(base, params=query, timeout=60)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code} for {state}{county}", file=sys.stderr)
            return 1
        rows = resp.json()
        frames.append(pd.DataFrame(rows[1:], columns=rows[0]))
        time.sleep(0.5)
    df = pd.concat(frames, ignore_index=True)
    df.insert(0, "GEOID", df["state"] + df["county"] + df["tract"])
    df = df.drop(columns=["state", "county", "tract"])
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"tract_moes_{args.year}.csv")
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    sidecar = {
        "kind": "census_api_response_table",
        "note": "Raw API values (sentinels kept) for the tracts of the listed counties. "
                "The API key is not part of the URLs.",
        "release": f"ACS 5-year {args.year}",
        "retrieved": dt.date.today().isoformat(),
        "rows": int(len(df)),
        "sha256": sha,
        "source_urls": urls,
        "variables": VARIABLES,
    }
    with open(path + ".json", "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
        f.write("\n")
    print(f"Wrote {path} ({len(df)} tracts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
