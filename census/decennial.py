# SPDX-License-Identifier: AGPL-3.0-or-later
"""2020 Decennial Census datasets and their uncertainty semantics.

Two products are available through the same client, cache, pipeline and
output formats as the ACS (``dataset='dec/pl'`` or ``'dec/dhc'``, or
``product='decennial'``, which selects the DHC):

* ``dec/pl``: the 2020 Census Redistricting Data (P.L. 94-171) Summary File
  (tables P1-P5, H1; down to census blocks).
* ``dec/dhc``: the 2020 Census Demographic and Housing Characteristics File
  (e.g. P1 total population, P12 sex by age, H1 housing units, H3 occupancy
  status). Note that table IDs are product-specific: P.L. 94-171 P1 is
  "Race", DHC P1 is "Total Population".

Uncertainty semantics (Round 15, C2). Decennial values are published counts.
They are **not sample-based**, so they have no sampling margin of error, and
they are **not error-free**: they carry coverage error (measured by the
Post-Enumeration Survey), processing error, and noise that the TopDown
differential-privacy algorithm of the Disclosure Avoidance System adds to
every count except the invariants. Outputs therefore never carry an MOE of 0
or a CV of 0: sampling MOE, SE and CV are undefined, and every extraction
records :data:`DISCLOSURE_AVOIDANCE_ADVISORY`.

Invariants (published without noise), per the Census Bureau's DAS update of
2020-11-25 (:data:`INVARIANTS_URL`) and the redistricting-data fact sheet
(:data:`DAS_FACT_SHEET_URL`): total population of each state (and DC, Puerto
Rico); total housing units per block; the number of (occupied) group-quarters
facilities by type per block. Everything else, including block, tract and
county population, is noisy.
"""

from typing import Any, Dict, Optional

DECENNIAL_PL = "dec/pl"
DECENNIAL_DHC = "dec/dhc"
DECENNIAL_DATASETS = (DECENNIAL_PL, DECENNIAL_DHC)
PRODUCT_DECENNIAL = "decennial"
# The 2020 Census only (the 2010 Census is out of scope, PLAN_ROUND_15 C4).
DECENNIAL_YEARS = (2020,)
DECENNIAL_YEAR = 2020
# PLAN_ROUND_15 D15-3. Blocks only for P.L. 94-171 and for invariant counts.
DECENNIAL_GEO_LEVELS = ("us", "state", "county", "tract", "block group", "block", "place")
# DHC tables allowed at block level: H1 (total housing units, a block invariant).
DHC_BLOCK_TABLES = ("H1",)

INVARIANTS_URL = (
    "https://www.census.gov/programs-surveys/decennial-census/decade/2020/planning-management/process/"
    "disclosure-avoidance/2020-das-updates/2020-11-25.html"
)
DAS_FACT_SHEET_URL = (
    "https://www.census.gov/content/dam/Census/library/factsheets/2021/"
    "protecting-the-confidentiality-of-the-2020-census-redistricting-data.pdf"
)
PPMF_URL = "https://www.census.gov/data/tables/2024/dec/2020-census-ppmf.html"
PES_URL = "https://www.census.gov/newsroom/blogs/random-samplings/2021/12/post-enumeration-measuring-coverage-error.html"
APPORTIONMENT_URL = "https://www.census.gov/data/tables/2020/dec/2020-apportionment-data.html"

UNCERTAINTY_LABEL = "not sample-based (MOE undefined)"
INVARIANTS = (
    "total population of each state (and DC, Puerto Rico)",
    "total housing units per census block",
    "number of occupied group-quarters facilities by type per census block",
)

DISCLOSURE_AVOIDANCE_ADVISORY = (
    "DECENNIAL CENSUS (2020): values are published counts, not sample-based: there is no sampling "
    "margin of error, so MOE, standard error, CV and reliability are undefined (never 0). The counts are "
    "not error-free: they carry coverage and processing error (Post-Enumeration Survey, "
    f"{PES_URL}) and disclosure-avoidance noise added by the TopDown differential-privacy algorithm. "
    "Only these counts are invariant (published without noise): " + "; ".join(INVARIANTS)
    + f" ({INVARIANTS_URL}). Small geographies and detailed cells are noisier; block-level "
    f"characteristics are noisy and should be aggregated before use ({DAS_FACT_SHEET_URL})."
)

BLOCK_ADVISORY = (
    "Census blocks: only total housing units (and occupied group-quarters facilities) are exact at block "
    "level; block population and characteristics carry disclosure-avoidance noise that does not shrink "
    "with block size. Aggregate blocks (block groups, tracts or custom areas) before analysis, and do not "
    "subtract one table from another at block level."
)

SAMPLING_REFUSAL = (
    "Decennial census counts (dec/pl, dec/dhc) are not sample-based: they have no sampling margin of "
    "error, so a sampling significance test, standard error, CV or reliability rating does not apply, "
    "and treating the MOE as 0 would overstate their precision. Their error comes from coverage and "
    "processing error and from disclosure-avoidance noise (TopDown differential privacy), which these "
    "tools do not measure. Compare decennial counts directly (aggregating small areas first), or test an "
    "ACS estimate against a decennial count used as a fixed reference (product1='5-year', "
    "product2='decennial')."
)

MIXED_NOTE = (
    "ACS vs decennial comparison: the test accounts only for the ACS sampling error; the decennial "
    "count is treated as a fixed reference with no sampling error. It does not measure the decennial "
    "count's disclosure-avoidance noise or coverage error, nor differences in concept and timing (ACS "
    "estimates describe a period and use the ACS residence rule; the census counts usual residence on "
    "April 1, 2020)."
)

# Explicit names only: generic words such as "census" or "dec" are not taken to mean the 2020 Census.
_PRODUCT_ALIASES = (PRODUCT_DECENNIAL, "decennial census", "2020 census") + DECENNIAL_DATASETS


def is_decennial(value: Optional[Any]) -> bool:
    """True for a decennial dataset key ('dec/pl', 'dec/dhc'), the 'decennial' product, or its
    spelled-out names ('decennial census', '2020 census')."""
    if value is None:
        return False
    return str(value).strip().lower() in _PRODUCT_ALIASES


def refuse_sampling(*products: Optional[Any]) -> None:
    """Raise ValueError (:data:`SAMPLING_REFUSAL`) when any product/dataset is decennial."""
    if any(is_decennial(p) for p in products):
        raise ValueError(SAMPLING_REFUSAL)


def uncertainty_record(geo_level: Optional[str] = None) -> Dict[str, Any]:
    """Machine-readable uncertainty statement written into decennial extraction metadata."""
    record: Dict[str, Any] = {
        "sample_based": False,
        "sampling_moe": None,
        "sampling_moe_status": UNCERTAINTY_LABEL,
        "error_sources": [
            "coverage error (Post-Enumeration Survey)",
            "processing error",
            "disclosure-avoidance noise (TopDown differential privacy)",
        ],
        "invariants": list(INVARIANTS),
        "advisory": DISCLOSURE_AVOIDANCE_ADVISORY,
        "sources": [INVARIANTS_URL, DAS_FACT_SHEET_URL, PES_URL],
    }
    if geo_level == "block":
        record["block_advisory"] = BLOCK_ADVISORY
    return record


def codebook_uncertainty(kind: str) -> str:
    """Codebook ``Uncertainty`` cell for a decennial column of ``kind`` (classify_column kind)."""
    if kind == "N":
        return (
            f"{UNCERTAINTY_LABEL}; subject to disclosure-avoidance noise (TopDown differential privacy), "
            "coverage and processing error. Invariant only for state total population, block housing "
            "units and occupied group-quarters facilities."
        )
    return ""
