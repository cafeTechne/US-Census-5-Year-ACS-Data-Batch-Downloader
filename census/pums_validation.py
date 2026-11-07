# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reproduce the Census PUMS verification estimates (PLAN_ROUND_10 workstream B).

Census publishes, with every PUMS release, a file of selected weighted
estimates and their replicate-weight SEs and 90% MOEs computed from that same
PUMS, by state:

    https://www2.census.gov/programs-surveys/acs/tech_docs/pums/estimates/pums_estimates_<YY>.csv
        (1-year, e.g. pums_estimates_24.csv for 2024)
    https://www2.census.gov/programs-surveys/acs/tech_docs/pums/estimates/pums_estimates_<YY-4>_<YY>.csv
        (5-year, e.g. pums_estimates_20_24.csv for 2020-2024)

(directory listing checked 2026-11-03). Columns: STATE, STATENAME,
CHARACTERISTIC, PUMS_EST_<tag>, PUMS_SE_<tag>, PUMS_MOE_<tag>. Values are
published as integers, so the reproduction tolerance is rounding: |ours -
published| <= 0.5 (plus float noise) for estimates, SEs and MOEs.

:data:`CHARACTERISTICS` maps each published characteristic (2019+ labels, which
use RELSHIPP) to a PUMS record type and a filter; :func:`reproduce` tabulates
them with :func:`census.pums.tabulate` (statistic='count'). Characteristics
without a mapping are reported as ``unmapped``, never guessed.
"""

import io
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from . import pums as P

VERIFICATION_BASE = P.PUMS_DOCS_BASE + "/estimates"
ROUNDING_TOLERANCE = 0.5 + 1e-6

# characteristic -> (record, where-expression or None)
CHARACTERISTICS: Dict[str, Tuple[str, Optional[str]]] = {
    "Total population": ("person", None),
    "Housing unit population (RELSHIPP=20-36)": ("person", "RELSHIPP >= 20 and RELSHIPP <= 36"),
    "GQ population (RELSHIPP=37-38)": ("person", "RELSHIPP >= 37 and RELSHIPP <= 38"),
    "GQ institutional population (RELSHIPP=37)": ("person", "RELSHIPP == 37"),
    "GQ noninstitutional population (RELSHIPP=38)": ("person", "RELSHIPP == 38"),
    "Total males (SEX=1)": ("person", "SEX == 1"),
    "Total females (SEX=2)": ("person", "SEX == 2"),
    "Age 0-4": ("person", "AGEP <= 4"),
    "Age 5-9": ("person", "AGEP >= 5 and AGEP <= 9"),
    "Age 10-14": ("person", "AGEP >= 10 and AGEP <= 14"),
    "Age 15-19": ("person", "AGEP >= 15 and AGEP <= 19"),
    "Age 20-24": ("person", "AGEP >= 20 and AGEP <= 24"),
    "Age 25-34": ("person", "AGEP >= 25 and AGEP <= 34"),
    "Age 35-44": ("person", "AGEP >= 35 and AGEP <= 44"),
    "Age 45-54": ("person", "AGEP >= 45 and AGEP <= 54"),
    "Age 55-59": ("person", "AGEP >= 55 and AGEP <= 59"),
    "Age 60-64": ("person", "AGEP >= 60 and AGEP <= 64"),
    "Age 65-74": ("person", "AGEP >= 65 and AGEP <= 74"),
    "Age 75-84": ("person", "AGEP >= 75 and AGEP <= 84"),
    "Age 85 and over": ("person", "AGEP >= 85"),
    "Total housing units (TYPEHUGQ=1)": ("housing", "TYPEHUGQ == 1"),
    "Total occupied units": ("housing", "TYPEHUGQ == 1 and TEN >= 1 and TEN <= 4"),
    "Owner occupied units (TEN in 1,2)": ("housing", "TYPEHUGQ == 1 and (TEN == 1 or TEN == 2)"),
    "Renter occupied units (TEN in 3,4)": ("housing", "TYPEHUGQ == 1 and (TEN == 3 or TEN == 4)"),
    "Owned with a mortgage (TEN=1)": ("housing", "TYPEHUGQ == 1 and TEN == 1"),
    "Owned free and clear (TEN=2)": ("housing", "TYPEHUGQ == 1 and TEN == 2"),
    "Rented for cash (TEN=3)": ("housing", "TYPEHUGQ == 1 and TEN == 3"),
    "No cash rent (TEN=4)": ("housing", "TYPEHUGQ == 1 and TEN == 4"),
    "Total vacant units": ("housing", "TYPEHUGQ == 1 and VACS >= 1 and VACS <= 7"),
    "For rent (VACS=1)": ("housing", "TYPEHUGQ == 1 and VACS == 1"),
    "For sale only (VACS=3)": ("housing", "TYPEHUGQ == 1 and VACS == 3"),
    "All Other Vacant (VACS in 2,4,5,6,7)": ("housing", "TYPEHUGQ == 1 and VACS in [2, 4, 5, 6, 7]"),
}
PERSON_COLUMNS = ["RELSHIPP", "SEX", "AGEP"]
HOUSING_COLUMNS = ["TYPEHUGQ", "TEN", "VACS"]


def verification_url(year: int, product: str = "5-year") -> str:
    p, yy = P._product(product), int(year) % 100
    tag = f"{yy:02d}" if p == "1-year" else f"{(int(year) - 4) % 100:02d}_{yy:02d}"
    return f"{VERIFICATION_BASE}/pums_estimates_{tag}.csv"


def parse_verification(raw: Union[bytes, str], state: Optional[Union[str, int]] = None) -> pd.DataFrame:
    """Published verification rows: STATE, STATENAME, CHARACTERISTIC, published_estimate/se/moe."""
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    df = pd.read_csv(io.StringIO(text), dtype={"STATE": str})
    rename = {}
    for c in df.columns:
        m = re.fullmatch(r"PUMS_(EST|SE|MOE)_.+", c)
        if m:
            rename[c] = {"EST": "published_estimate", "SE": "published_se", "MOE": "published_moe"}[m.group(1)]
    df = df.rename(columns=rename)
    df["STATE"] = df["STATE"].str.zfill(2)
    if state is not None:
        df = df[df["STATE"] == P.state_fips(state)]
    return df.reset_index(drop=True)


def fetch_verification(year: int, product: str = "5-year", state: Optional[Union[str, int]] = None, *,
                       cache_dir: Optional[str] = None, refresh: bool = False, session: Any = None) -> pd.DataFrame:
    url = verification_url(year, product)
    path, meta = P.cached_download(url, "verification", cache_dir=cache_dir, refresh=refresh, session=session)
    with open(path, "rb") as f:
        out = parse_verification(f.read(), state)
    out.attrs.update(source_url=url, sha256=meta.get("sha256"))
    return out


def reproduce(published: pd.DataFrame, person: Optional[pd.DataFrame], housing: Optional[pd.DataFrame],
              tolerance: float = ROUNDING_TOLERANCE) -> pd.DataFrame:
    """Tabulate each published characteristic and compare (one state's rows).

    Adds ``estimate``, ``se``, ``moe``, the absolute errors, ``n_unweighted``,
    ``status`` ('match', 'mismatch', 'unmapped' or 'no_data').
    """
    rows = []
    for rec in published.to_dict(orient="records"):
        spec = CHARACTERISTICS.get(rec["CHARACTERISTIC"])
        out = dict(rec)
        frame = None if spec is None else (person if spec[0] == "person" else housing)
        if spec is None or frame is None:
            out.update(status="unmapped" if spec is None else "no_data")
            rows.append(out)
            continue
        t = P.tabulate(frame, "count", where=spec[1], weight=spec[0]).iloc[0]
        out.update(record=spec[0], where=spec[1], estimate=float(t["estimate"]), se=float(t["se"]),
                   moe=float(t["moe"]), n_unweighted=int(t["n_unweighted"]))
        for k in ("estimate", "se", "moe"):
            out[f"abs_error_{k}"] = abs(out[k] - float(rec[f"published_{k}"]))
        ok = all(out[f"abs_error_{k}"] <= tolerance for k in ("estimate", "se", "moe"))
        out["status"] = "match" if ok else "mismatch"
        rows.append(out)
    return pd.DataFrame(rows)


def summarize(result: pd.DataFrame) -> Dict[str, Any]:
    done = result[result["status"].isin(["match", "mismatch"])]
    return {
        "characteristics": int(len(result)),
        "compared": int(len(done)),
        "matched": int((done["status"] == "match").sum()),
        "unmapped": result.loc[result["status"] == "unmapped", "CHARACTERISTIC"].tolist(),
        "max_abs_error_estimate": float(done["abs_error_estimate"].max()) if len(done) else np.nan,
        "max_abs_error_se": float(done["abs_error_se"].max()) if len(done) else np.nan,
        "max_abs_error_moe": float(done["abs_error_moe"].max()) if len(done) else np.nan,
    }


def validate_release(state: Union[str, int], year: int, product: str = "5-year", **kwargs: Any
                     ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Download (cached) the state PUMS files and verification CSV, then reproduce."""
    pub = fetch_verification(year, product, state, **kwargs)
    person = P.fetch_pums(state, year, product, "person", PERSON_COLUMNS, **kwargs)
    housing = P.fetch_pums(state, year, product, "housing", HOUSING_COLUMNS, **kwargs)
    res = reproduce(pub, person, housing)
    summary = summarize(res)
    summary.update(state=P.state_fips(state), release=P.release_label(int(year), product),
                   verification_url=pub.attrs.get("source_url"), verification_sha256=pub.attrs.get("sha256"),
                   person_url=person.attrs.get("source_url"), person_sha256=person.attrs.get("sha256"),
                   housing_url=housing.attrs.get("source_url"), housing_sha256=housing.attrs.get("sha256"))
    return res, summary


def cell_aggregate(frame: pd.DataFrame, columns: Sequence[str], weight: str) -> pd.DataFrame:
    """Collapse records to unique ``columns`` cells, summing the 81 weights and
    counting records (``n_records``). Counts tabulated from the aggregate equal
    those from the records, so this is a compact frozen fixture for count
    verification (it cannot reproduce means or medians)."""
    base = P.weight_column(weight)
    wcols = [base] + [f"{base}{i}" for i in range(1, 81)]
    keys = [frame[c].fillna(-1) for c in columns]
    g = frame[wcols].astype("float64").groupby(keys, sort=True)
    agg = g.sum()
    agg["n_records"] = g.size()
    agg = agg.reset_index()
    for c in columns:
        agg[c] = agg[c].where(agg[c] != -1, np.nan)
    return agg[list(columns) + ["n_records"] + wcols]
