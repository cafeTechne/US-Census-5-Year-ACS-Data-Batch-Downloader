# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end batch extraction pipeline shared by the GUI, MCP server, CLI and SDK.

    plan:     per-vintage metadata -> concordance (+ reviewed overrides)
              -> variable selection -> continuity check -> block or proceed
    download: raw API rows -> GEOID -> sentinel cleaning -> R-CPI-U-RS deflation
              -> SE/CV -> concept-keyed columns
    write:    legacy CSV/Excel, or tidy (GEOID, Year) panel / Parquet
              + concordance.csv, codebook.csv, extraction metadata JSON

See docs/agent_protocol.md for how agents should act on plan results.
"""

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

import pandas as pd

from census import errors
from census.api import (
    ACS1_AREAS_URL,
    ACS1_EXPERIMENTAL_URL,
    ACS1_EXPERIMENTAL_YEAR,
    ACS1_GEO_LEVELS,
    ACS1_MIN_POPULATION,
    BLOCK_GROUP,
    BLOCK_GROUP_FIRST_YEAR,
    COMPARING_ACS_URL,
    GEO_LEVELS,
    MIXED_PRODUCTS_MESSAGE,
    PRODUCT_1YR,
    PRODUCT_5YR,
    CensusAPIClient,
    DATASETS,
    dataset_for_product,
    dataset_product,
    infer_dataset,
    normalize_geo_level,
    normalize_product,
)
from census.cache import get_default_cache
from census.concordance import (
    ISSUE_METADATA_UNAVAILABLE,
    NORMALIZATION_VERSION,
    Concordance,
    apply_overrides,
    build_concordance,
    crosswalk_path,
)
from census.continuity import continuity_check
from census.data_integrity import BundledDataIntegrityError, integrity_error_result
from census.decennial import (
    BLOCK_ADVISORY,
    DECENNIAL_DATASETS,
    DECENNIAL_DHC,
    DECENNIAL_GEO_LEVELS,
    DECENNIAL_PL,
    DECENNIAL_YEARS,
    DHC_BLOCK_TABLES,
    DISCLOSURE_AVOIDANCE_ADVISORY,
    PRODUCT_DECENNIAL,
    is_decennial,
    uncertainty_record,
)
from census.export import (
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)
from census.geo import (
    STATE_MAPPING,
    add_geoid_column,
    zctas_for_state,
)
from census.inflation import (
    DEFAULT_PRICE_INDEX,
    INDEX_DISPLAY,
    PRICE_INDEXES,
    PriceIndexUnavailable,
    boundary_shift_advisory,
    deflate_dataframe,
    dollar_columns,
    get_cpi,
    home_value_advisory,
    provisional_advisory,
    price_index_info,
)
from census.metadata import load_manifest, table_universe, table_variables
from census.panel import (
    IDENTIFIER_COLUMNS,
    build_tidy_panel,
    classify_column,
    describe_columns,
    export_parquet,
    generate_codebook,
    make_slug_map,
)
from census.stats import add_reliability_columns, clean_census_sentinels, parse_variable_code
from census.tables import load_variable_metadata

PACKAGE_VERSION = "0.15.1"

CONCORDANCE_MODES = ("fail", "strict", "code")
MAX_ISSUES_IN_RESULT = 100

LEGACY_FORMATS = ("both", "wide_only", "melted_only", "excel")
PANEL_FORMATS = ("panel", "parquet")
OUTPUT_FORMATS = LEGACY_FORMATS + PANEL_FORMATS

GEO_LEVEL_NAMES = ("us", "state", "county", "tract", "block group", "block", "place", "zcta", "msa")
# Release whose county list enumerates block-group requests when no counties
# are given (one API request per county).
BLOCK_GROUP_COUNTY_LIST_YEAR = 2023

# ZCTAs per API request when querying explicit ZCTA lists
ZCTA_CHUNK_SIZE = 50

# state_code value that expands to every state (+ DC; Puerto Rico opt-in)
NATIONWIDE = "all"
PUERTO_RICO = "72"
# 50 states + DC. Island areas (60, 66, 69, 74, 78) are not in the ACS.
ACS_STATE_CODES = tuple(c for c in STATE_MAPPING if int(c) <= 56)
NON_ACS_TERRITORIES = tuple(c for c in STATE_MAPPING if int(c) > 56 and c != PUERTO_RICO)
NATIONWIDE_LABEL = "All_States"

# Extraction metadata layout version; replay requires these fields.
REPLAY_SCHEMA_VERSION = 1
REPLAY_REQUIRED_FIELDS = (
    "replay_schema_version",
    "table_code",
    "dataset",
    "years",
    "entities",
    "output_format",
    "options",
    "variables",
)

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str, int], None]


@dataclass
class ResearchOptions:
    """Research-grade processing switches for a batch extraction."""

    clean_sentinels: bool = True
    add_sentinel_notes: bool = False
    compute_reliability: bool = False
    inflation_base_year: Optional[int] = None
    include_codebook: bool = False
    use_cache: bool = True
    slug_style: str = "label"
    cpi_table: Optional[Dict[int, float]] = None
    # R-CPI-U-RS matches the Census Bureau's own ACS adjustments; "cpi-u" also available.
    price_index: str = DEFAULT_PRICE_INDEX
    # Use index years whose annual figure rests on fewer than 12 published months
    # (2025: October never published). Adds a PROVISIONAL INDEX advisory.
    allow_provisional_index: bool = False
    # Apportion tract panels spanning the 2010/2020 tract boundary to one tract
    # vintage (2010 or 2020) with census.geo_crosswalk (land-area weights).
    harmonize_tracts_to: Optional[int] = None
    # "fail": stop when unresolved cross-vintage issues touch selected variables.
    # "strict": keep unresolved items as separate columns and report them.
    # "code": key columns by raw code (single-year, or allow_unverified_concordance).
    concordance_mode: str = "fail"
    continuity_check: bool = True
    allow_unverified_concordance: bool = False
    # ACS product: "5-year" (default) or "1-year" (acs1 datasets; areas of
    # 65,000+ only; 1-year and 5-year estimates are never compared), or
    # "decennial" (2020 Census, dec/dhc by default or dataset='dec/pl'; counts
    # that are not sample-based, census.decennial).
    product: str = PRODUCT_5YR
    # 1-year only: the 2020 1-year release is unavailable (Census published only
    # experimental estimates, outside the API). True drops 2020 from the request
    # with an advisory instead of refusing the plan.
    allow_experimental_2020: bool = False


@dataclass
class Entity:
    """One API request target (may cover many rows, e.g. all tracts in a county)."""

    name: str
    geo_level: str
    state_code: Optional[str] = None
    place_code: Optional[str] = None
    county_code: Optional[str] = None
    tract_code: Optional[str] = None
    zcta_code: Optional[str] = None
    msa_code: Optional[str] = None
    group: str = ""  # output folder label; chunks of one request share a group
    # Releases this target exists in (None = every release). Block-group county
    # lists differ by release (Connecticut planning regions from 2022, Alaska
    # 02261 split in 2020, South Dakota 46113 -> 46102 in 2015).
    years: Optional[Tuple[int, ...]] = None

    def __post_init__(self) -> None:
        if not self.group:
            self.group = self.name

    def applies(self, year: int) -> bool:
        return self.years is None or int(year) in self.years

    def request_kwargs(self) -> Dict[str, Any]:
        return {
            "geo_level": self.geo_level,
            "state_code": self.state_code,
            "place_code": self.place_code,
            "county_code": self.county_code,
            "tract_code": self.tract_code,
            "zcta_code": self.zcta_code,
            "msa_code": self.msa_code,
        }


def _join(codes: Optional[Sequence[str]]) -> Optional[str]:
    if not codes:
        return None
    return ",".join(str(c).strip() for c in codes if str(c).strip()) or None


def is_nationwide(state_code: Optional[str]) -> bool:
    """True when ``state_code`` asks for every state ("all")."""
    return isinstance(state_code, str) and state_code.strip().lower() == NATIONWIDE


def expand_states(state_code: Optional[str], include_pr: bool = False) -> List[str]:
    """Expand a state selection to 2-digit FIPS codes.

    "all" -> the 50 states + DC (+ Puerto Rico "72" when ``include_pr``).
    A code or comma-separated list is validated and returned in order. Island
    areas (American Samoa, Guam, Northern Mariana Islands, U.S. Virgin Islands,
    Minor Outlying Islands) are rejected: the ACS does not cover them.
    """
    if state_code is None or not str(state_code).strip():
        raise ValueError("state_code is required (a 2-digit FIPS code or 'all').")
    if is_nationwide(state_code):
        return list(ACS_STATE_CODES) + ([PUERTO_RICO] if include_pr else [])
    out: List[str] = []
    for part in str(state_code).split(","):
        code = part.strip()
        if not code:
            continue
        if code.isdigit() and len(code) == 1:
            code = code.zfill(2)
        if code in NON_ACS_TERRITORIES:
            raise ValueError(
                f"State FIPS {code} ({STATE_MAPPING[code]}) is not covered by the ACS."
            )
        if code not in STATE_MAPPING:
            raise ValueError(f"Unknown state FIPS code '{code}'. Use a 2-digit code or 'all'.")
        if code not in out:
            out.append(code)
    if not out:
        raise ValueError("state_code is required (a 2-digit FIPS code or 'all').")
    return out


def _county_codes(state_code: str, client: Optional[CensusAPIClient],
                  year: int = BLOCK_GROUP_COUNTY_LIST_YEAR) -> List[str]:
    """County FIPS codes of a state in release ``year`` from the API (empty list
    when unavailable)."""
    if client is None:
        client = CensusAPIClient(cache=get_default_cache())
    try:
        rows = client.list_geographies(int(year), "county", state_code=state_code)
    except Exception:  # noqa: BLE001 - network or parsing failure: caller falls back
        return []
    return sorted({str(r.get("county", "")).zfill(3) for r in rows if r.get("county")})


def _block_group_entities(
    state_code: str,
    codes: Optional[Sequence[str]],
    names: Mapping[str, str],
    client: Optional[CensusAPIClient],
    group: str = "",
    years: Optional[Sequence[int]] = None,
) -> List[Entity]:
    """One request per county (block groups nest in tracts within counties).

    Without ``codes`` the county list comes from the API for each release in
    ``years`` (default ``BLOCK_GROUP_COUNTY_LIST_YEAR``), so counties that
    exist only in some releases are requested only for those; a year whose
    list cannot be fetched gets one state-wide request (``county:*``).
    """
    state_name = STATE_MAPPING.get(state_code, f"State_{state_code}")
    if not group:
        group = f"{state_name}_Block_Groups" if codes else f"{state_name}_All_Block_Groups"

    def county_entity(c: str, yrs: Optional[Tuple[int, ...]] = None) -> Entity:
        return Entity(f"{names.get(c, state_name + '_County_' + c)}_Block_Groups", BLOCK_GROUP, state_code,
                      county_code=c, group=group, years=yrs)

    if codes:
        return [county_entity(str(c).strip().zfill(3)) for c in codes]
    list_years = sorted({int(y) for y in years}) if years else [BLOCK_GROUP_COUNTY_LIST_YEAR]
    by_year = {y: _county_codes(state_code, client, y) for y in list_years}
    unlisted = tuple(y for y in list_years if not by_year[y])
    listed = [y for y in list_years if by_year[y]]
    out: List[Entity] = []
    if unlisted:
        out.append(Entity(f"{state_name}_All_Block_Groups", BLOCK_GROUP, state_code, county_code="*",
                          group=group, years=unlisted if listed else None))
    county_years: Dict[str, List[int]] = {}
    for y in listed:
        for c in by_year[y]:
            county_years.setdefault(c, []).append(y)
    for c in sorted(county_years):
        yrs = tuple(county_years[c])
        out.append(county_entity(c, None if len(yrs) == len(list_years) else yrs))
    return out


def _nationwide_entities(
    geo_level: str, codes: Optional[Sequence[str]], include_pr: bool,
    client: Optional[CensusAPIClient] = None, years: Optional[Sequence[int]] = None,
) -> List[Entity]:
    """Entities for state_code='all'.

    County, place and state levels are one request per year with an explicit
    state list (``in=state:01,02,...``), which the API accepts for every ACS
    5-year vintage; ``state:*`` would also return Puerto Rico. Tracts cannot use
    a state wildcard, so they are one request per state, sharing one group.
    """
    if geo_level == "block":
        raise ValueError("Census blocks are requested one state at a time; give a state_code (and county codes).")
    states = expand_states(NATIONWIDE, include_pr)
    joined = ",".join(states)
    label = NATIONWIDE_LABEL + ("_and_PR" if include_pr else "")
    if geo_level in ("county", "place", "tract", BLOCK_GROUP) and codes:
        raise ValueError(
            f"{geo_level} codes are only unique within a state; with state_code='all' "
            "omit the codes (all geographies nationwide) or give a single state."
        )
    if geo_level == "state":
        return [Entity(label, "state", joined)]
    if geo_level == "county":
        return [Entity(f"{label}_All_Counties", "county", joined, county_code="*")]
    if geo_level == "place":
        return [Entity(f"{label}_All_Places", "place", joined, place_code="*")]
    if geo_level == "tract":
        group = f"{label}_All_Tracts"
        return [
            Entity(f"{STATE_MAPPING[s]}_All_Tracts", "tract", s, county_code="*", group=group)
            for s in states
        ]
    if geo_level == BLOCK_GROUP:
        out: List[Entity] = []
        for s in states:
            out.extend(_block_group_entities(s, None, {}, client, group=f"{label}_All_Block_Groups",
                                             years=years))
        return out
    # us, msa, zcta are national requests already; the state filter does not apply.
    return build_entities(geo_level, None, codes)


def build_entities(
    geo_level: str,
    state_code: Optional[str] = None,
    codes: Optional[Sequence[str]] = None,
    names: Optional[Mapping[str, str]] = None,
    include_pr: bool = False,
    client: Optional[CensusAPIClient] = None,
    years: Optional[Sequence[int]] = None,
) -> List[Entity]:
    """Translate a geography selection into API request entities.

    ``years`` (block groups without county codes): list counties for each
    release so county changes between releases are requested correctly.

    codes meaning by level:
        place  -> place FIPS codes (required; one request each)
        county -> county FIPS codes (optional; default all counties in state)
        tract  -> county FIPS codes whose tracts to fetch (default: all counties)
        block group (aliases block_group, bg, 150) -> county FIPS codes; one
                  request per county (default: every county, listed with
                  ``client``); GEOIDs are 12 digits
        block  -> 2020 Census only: county FIPS codes (one request each;
                  default one state-wide request); GEOIDs are 15 digits
        zcta   -> 5-digit ZCTAs (default: ZIPs in state via zip2fips.json, or
                  all ZCTAs nationally when no state is given)
        msa    -> CBSA codes (default: all metro/micro areas)
        state, us -> ignored

    state_code="all" selects every state + DC (Puerto Rico with include_pr):
    one request per year for state, county and place (no codes allowed), one
    per state per year for tract; us, msa and zcta are national regardless.
    """
    names = names or {}
    geo_level = normalize_geo_level(geo_level)
    if geo_level not in GEO_LEVEL_NAMES:
        raise ValueError(
            f"Unsupported geo_level: {geo_level}. Choose from {list(GEO_LEVEL_NAMES)}."
        )
    if is_nationwide(state_code):
        return _nationwide_entities(geo_level, codes, include_pr, client, years)
    state_name = STATE_MAPPING.get(state_code or "", f"State_{state_code}")
    if geo_level in ("place", "county", "tract", "state", BLOCK_GROUP) and not state_code:
        raise ValueError(f"state_code is required for geo_level '{geo_level}'.")
    if geo_level == BLOCK_GROUP:
        bg_states = expand_states(state_code)
        if len(bg_states) > 1 and codes:
            raise ValueError("block-group county codes need a single state_code.")
        bg: List[Entity] = []
        for st in bg_states:
            bg.extend(_block_group_entities(st, codes, names, client, years=years))
        return bg
    if geo_level == "block":
        # 2020 Census only: one request per county given, else one per state (county:*).
        blk_states = expand_states(state_code)
        if len(blk_states) > 1 and codes:
            raise ValueError("block county codes need a single state_code.")
        blocks: List[Entity] = []
        for st in blk_states:
            st_name = STATE_MAPPING.get(st, f"State_{st}")
            if not codes:
                blocks.append(Entity(f"{st_name}_All_Blocks", "block", st, county_code="*"))
                continue
            blocks.extend(
                Entity(f"{names.get(c, st_name + '_County_' + c)}_Blocks", "block", st,
                       county_code=str(c).strip().zfill(3), group=f"{st_name}_Blocks")
                for c in codes
            )
        return blocks

    if geo_level == "place":
        if not codes:
            raise ValueError("place codes must be provided when geo_level is 'place'.")
        return [
            Entity(names.get(c, f"Place_{c}"), "place", state_code, place_code=c)
            for c in codes
        ]
    if geo_level == "state":
        return [Entity(state_name, "state", state_code)]
    if geo_level == "us":
        return [Entity("United_States", "us")]
    if geo_level == "county":
        joined = _join(codes)
        label = f"{state_name}_All_Counties" if not joined else f"{state_name}_Counties"
        return [Entity(label, "county", state_code, county_code=joined or "*")]
    if geo_level == "tract":
        if not codes:
            return [Entity(f"{state_name}_All_Tracts", "tract", state_code, county_code="*")]
        return [
            Entity(
                f"{names.get(c, 'County_' + c)}_Tracts",
                "tract",
                state_code,
                county_code=c,
            )
            for c in codes
        ]
    if geo_level == "msa":
        joined = _join(codes)
        label = "All_CBSAs" if not joined else "Selected_CBSAs"
        return [Entity(label, "msa", msa_code=joined or "*")]

    # zcta
    zctas = list(codes) if codes else (zctas_for_state(state_code) if state_code else [])
    if not zctas:
        return [Entity("All_ZCTAs", "zcta", zcta_code="*")]
    group = f"{state_name}_ZCTAs" if state_code else "Selected_ZCTAs"
    chunks = [zctas[i : i + ZCTA_CHUNK_SIZE] for i in range(0, len(zctas), ZCTA_CHUNK_SIZE)]
    return [
        Entity(
            group if len(chunks) == 1 else f"{group}_part{i + 1}",
            "zcta",
            state_code,
            zcta_code=",".join(chunk),
            group=group,
        )
        for i, chunk in enumerate(chunks)
    ]


def resolve_metadata(
    table_code: str,
    dataset: str,
    year: int,
    client: Optional[CensusAPIClient] = None,
) -> Dict[str, Dict[str, str]]:
    """Variable metadata for a table in one vintage (archive, then API).

    Falls back to the latest archived detailed-table metadata only when the
    vintage itself is unavailable; callers needing vintage-exact labels should
    use census.metadata.table_variables and check its source.
    """
    variables, _ = table_variables(table_code, dataset, year, client)
    if variables:
        return variables
    if dataset == "acs5":
        prefix = f"{table_code}_"
        return {k: v for k, v in load_variable_metadata().items() if k.startswith(prefix)}
    return {}


def prepare_frame(
    raw: List[List[str]],
    year: int,
    options: ResearchOptions,
    metadata: Mapping[str, Mapping[str, str]],
    geo_level: Optional[str] = None,
) -> pd.DataFrame:
    """Apply GEOID, sentinel cleaning, deflation and SE/CV to one API response.

    Columns keep the year's raw Census variable codes; ``metadata`` must be the
    same vintage's metadata (it decides which columns are dollar values).
    """
    df = pd.DataFrame(raw[1:], columns=raw[0])
    # get=NAME,group(X) returns NAME twice; keep the first of any duplicate.
    df = df.loc[:, ~df.columns.duplicated()]
    if geo_level == BLOCK_GROUP and {"state", "county", "tract", BLOCK_GROUP}.issubset(df.columns):
        geoid = (
            df["state"].astype(str).str.zfill(2) + df["county"].astype(str).str.zfill(3)
            + df["tract"].astype(str).str.zfill(6) + df[BLOCK_GROUP].astype(str).str[-1:]
        )
        df = df.drop(columns=["GEOID"], errors="ignore")
        df.insert(0, "GEOID", geoid.values)
    else:
        df = add_geoid_column(df, geo_level)
    if options.clean_sentinels:
        df = clean_census_sentinels(df, add_notes=options.add_sentinel_notes)
    if options.inflation_base_year:
        cols = dollar_columns(df.columns, metadata)
        if cols:
            # Sentinels must never be scaled into plausible-looking values.
            df = clean_census_sentinels(df, columns=cols)
            df = deflate_dataframe(
                df,
                year,
                options.inflation_base_year,
                cols,
                options.cpi_table,
                options.price_index,
                options.allow_provisional_index,
            )
    if options.compute_reliability:
        df = add_reliability_columns(df)
    return df


def _rename_column(col: str, mapping: Mapping[str, str]) -> Optional[str]:
    """New name for a column under a base->representative mapping (None = drop)."""
    if col.endswith("_NOTE"):
        inner = _rename_column(col[: -len("_NOTE")], mapping)
        return f"{inner}_NOTE" if inner else None
    parsed = parse_variable_code(col)
    if parsed:
        base, suffix = parsed
        return f"{mapping[base]}{suffix}" if base in mapping else None
    info = classify_column(col)
    if info and info[1] in ("SE", "CV", "RELIABILITY", "CONTROLLED"):
        base, kind, pct = info
        return f"{mapping[base]}{'P' if pct else ''}_{kind}" if base in mapping else None
    return col  # identifiers and geography columns


def apply_concordance(df: pd.DataFrame, mapping: Mapping[str, str]) -> pd.DataFrame:
    """Rename one year's columns to concept-representative codes.

    Variables outside ``mapping`` (unselected, excluded, or unknown to the
    vintage metadata) are dropped.
    """
    renames: Dict[str, str] = {}
    keep: List[str] = []
    for col in df.columns:
        new = _rename_column(str(col), mapping)
        if new is None:
            continue
        keep.append(col)
        renames[col] = new
    out = df[keep].rename(columns=renames)
    if out.columns.duplicated().any():
        dupes = sorted(set(out.columns[out.columns.duplicated()]))
        raise AssertionError(f"Concordance mapped several columns to {dupes[:5]}")
    return out


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _is_blocking(issue: Any, multi_year: bool) -> bool:
    if issue.kind == ISSUE_METADATA_UNAVAILABLE and not multi_year:
        return False
    return issue.blocking


def _issue_dicts(issues: Sequence[Any], multi_year: bool) -> List[Dict[str, Any]]:
    out = []
    for issue in issues:
        d = issue.to_dict()
        d["blocking"] = _is_blocking(issue, multi_year)
        out.append(d)
    return out


def _price_index_error(options: ResearchOptions, years: Sequence[int]) -> Optional[Dict[str, Any]]:
    if not options.inflation_base_year:
        return None
    if options.price_index not in PRICE_INDEXES:
        return errors.error_result(
            errors.INVALID_INPUT,
            f"price_index must be one of {PRICE_INDEXES}.",
        )
    try:
        for year in list(years) + [options.inflation_base_year]:
            get_cpi(year, options.cpi_table, options.price_index, options.allow_provisional_index)
    except PriceIndexUnavailable as exc:
        return errors.error_result(errors.PRICE_INDEX_UNAVAILABLE, str(exc.args[0]))
    except BundledDataIntegrityError as exc:
        return integrity_error_result(exc)
    return None


def _errata_summary(table_code: str, years: Sequence[int]) -> Any:
    """Census errata and user notes affecting this table and these releases."""
    try:
        from census.errata import errata_summary

        return errata_summary(table_code, years)
    except Exception as exc:  # noqa: BLE001 - errata lookup must never block a plan
        return {"error": f"errata lookup unavailable: {exc}"}


def _decennial_plan_error(
    table_code: str, dataset: str, years: Sequence[int], entities: Sequence[Entity], options: ResearchOptions
) -> Optional[Dict[str, Any]]:
    """Refusals specific to the 2020 Census datasets (None when the request is valid)."""
    other = [y for y in years if y not in DECENNIAL_YEARS]
    if other:
        return errors.error_result(
            errors.VINTAGE_UNAVAILABLE,
            f"The decennial datasets here cover the 2020 Census only ({dataset} {list(DECENNIAL_YEARS)}); "
            f"not available for {other}. Request years=[2020].",
            unavailable_years=other,
        )
    level = normalize_geo_level(entities[0].geo_level)
    if level not in DECENNIAL_GEO_LEVELS:
        return errors.error_result(
            errors.INVALID_GEO_LEVEL,
            f"2020 Census extractions support {list(DECENNIAL_GEO_LEVELS)}; not '{level}'.",
        )
    if level == "block" and dataset != DECENNIAL_PL and table_code not in DHC_BLOCK_TABLES:
        return errors.error_result(
            errors.INVALID_GEO_LEVEL,
            f"Blocks are offered for P.L. 94-171 tables (dataset='{DECENNIAL_PL}') and for the invariant "
            f"housing-unit count (DHC {list(DHC_BLOCK_TABLES)}), not {dataset} {table_code}: block-level "
            "characteristics are dominated by disclosure-avoidance noise. Request block groups or tracts.",
        )
    if options.harmonize_tracts_to is not None:
        return errors.error_result(
            errors.INVALID_INPUT,
            "harmonize_tracts_to does not apply to the 2020 Census (it is tabulated on 2020 tracts).",
        )
    if options.inflation_base_year:
        return errors.error_result(
            errors.INVALID_INPUT, "The 2020 Census P.L. 94-171 and DHC files publish no dollar values."
        )
    return None


def plan_extraction(
    table_code: str,
    entities: Sequence[Entity],
    years: Sequence[int],
    output_format: str = "panel",
    options: Optional[ResearchOptions] = None,
    dataset: Optional[str] = None,
    variables: Optional[Sequence[str]] = None,
    client: Optional[CensusAPIClient] = None,
    product: Optional[str] = None,
) -> Dict[str, Any]:
    """Dry run: validate, build the concordance, check continuity, count calls.

    ``product`` ('5-year' default, '1-year', or 'decennial' for the 2020
    Census; overrides ``options.product``) selects the release family; a generic ``dataset`` ('acs5', 'profile',
    ...) is mapped to the product's endpoint, and an explicit 1-year dataset
    ('acs1', 'acs1/subject', ...) implies '1-year'. 1-year plans probe which
    requested areas each release publishes (one cached listing call per year
    and state) and report unavailable ones under ``geography_availability``.

    Makes at most one national-level API call per year (for the continuity
    check; cached), and no data downloads. The returned dict is JSON-safe
    except for the private ``_concordance`` key, which run_batch reuses.
    """
    options = options or ResearchOptions()
    years = sorted(set(int(y) for y in years))
    table_code = table_code.strip().upper()

    if output_format not in OUTPUT_FORMATS:
        return errors.error_result(
            errors.INVALID_OUTPUT_FORMAT,
            f"Unsupported output_format '{output_format}'. Choose from {list(OUTPUT_FORMATS)}.",
        )
    if not years:
        return errors.error_result(errors.INVALID_INPUT, "No years requested.")
    if not entities:
        return errors.error_result(errors.INVALID_INPUT, "No geographic entities requested.")
    explicit_product = product is not None or options.product != PRODUCT_5YR
    requested_product = product if product is not None else options.product
    decennial = is_decennial(dataset) or is_decennial(requested_product)
    if decennial:
        if dataset and dataset not in DECENNIAL_DATASETS:
            return errors.error_result(
                errors.INVALID_DATASET,
                (f"Dataset '{dataset}' is an ACS dataset but product='decennial' was requested; "
                 if dataset in DATASETS else f"Dataset '{dataset}' is not a dataset key; ")
                + f"use dataset {list(DECENNIAL_DATASETS)} for the 2020 Census.",
            )
        if explicit_product and not is_decennial(requested_product):
            return errors.error_result(
                errors.INVALID_DATASET,
                f"Dataset '{dataset}' is the 2020 Census but product='{requested_product}' was requested. "
                "Decennial counts and ACS estimates are separate products.",
            )
        product = PRODUCT_DECENNIAL
        dataset = dataset if dataset in DECENNIAL_DATASETS else DECENNIAL_DHC
        decennial_error = _decennial_plan_error(table_code, dataset, years, entities, options)
        if decennial_error:
            return decennial_error
    else:
        try:
            product = normalize_product(requested_product)
        except ValueError as exc:
            return errors.error_result(errors.INVALID_INPUT, str(exc))
    if dataset and dataset in DATASETS and not decennial:
        ds_product = dataset_product(dataset)
        if ds_product != product:
            if not explicit_product:
                product = ds_product
            elif ds_product == PRODUCT_5YR:
                dataset = dataset_for_product(dataset, product)
            else:
                return errors.error_result(
                    errors.INVALID_DATASET,
                    f"Dataset '{dataset}' is {ds_product} but product='{product}' was requested. "
                    + MIXED_PRODUCTS_MESSAGE,
                )
    dataset = dataset or infer_dataset(table_code, product)
    if dataset not in DATASETS:
        return errors.error_result(
            errors.INVALID_DATASET,
            f"Unsupported dataset '{dataset}'."
            + (f" For the 2020 Census use dataset {list(DECENNIAL_DATASETS)} (or product='decennial')."
               if str(dataset).strip().lower().startswith(("dec", "census")) else ""),
        )
    options = replace(options, product=product)
    if options.concordance_mode not in CONCORDANCE_MODES:
        return errors.error_result(
            errors.INVALID_INPUT, f"concordance_mode must be one of {CONCORDANCE_MODES}."
        )
    price_error = _price_index_error(options, years)
    if price_error:
        return price_error

    if client is None:
        client = CensusAPIClient(cache=get_default_cache() if options.use_cache else None)

    geo_level = entities[0].geo_level
    if geo_level == "block" and not decennial:
        return errors.error_result(
            errors.INVALID_GEO_LEVEL,
            "Census blocks are published only in the 2020 Census datasets (dataset='dec/pl', or "
            "'dec/dhc' for H1 housing units), not in the ACS.",
        )
    excluded_years: List[int] = []
    acs1_advisories: List[str] = []
    if product == PRODUCT_1YR:
        if normalize_geo_level(geo_level) not in ACS1_GEO_LEVELS:
            return errors.error_result(
                errors.INVALID_GEO_LEVEL,
                f"ACS 1-year estimates are not published for '{geo_level}' (1-year levels here: "
                f"{list(ACS1_GEO_LEVELS)}; areas of {ACS1_MIN_POPULATION:,}+ only, {ACS1_AREAS_URL}). "
                "Use the 5-year product for small areas.",
            )
        if ACS1_EXPERIMENTAL_YEAR in years:
            if not options.allow_experimental_2020:
                return errors.error_result(
                    errors.VINTAGE_UNAVAILABLE,
                    "The 2020 ACS 1-year release is unavailable: because of COVID-19 data-collection "
                    "disruptions Census published only experimental 2020 1-year estimates (different "
                    f"weighting, not in the API; {ACS1_EXPERIMENTAL_URL}). Drop 2020, or pass "
                    "allow_experimental_2020=True to skip it with an advisory.",
                    unavailable_years=[ACS1_EXPERIMENTAL_YEAR],
                )
            years = [y for y in years if y != ACS1_EXPERIMENTAL_YEAR]
            excluded_years = [ACS1_EXPERIMENTAL_YEAR]
            acs1_advisories.append(
                "EXPERIMENTAL 2020: the 2020 ACS 1-year standard release does not exist; Census "
                "published experimental estimates (experimental weights, limited tables) only as "
                f"downloadable files ({ACS1_EXPERIMENTAL_URL}), not through the API. 2020 is excluded "
                "from this extraction; do not splice experimental tables into a 1-year series "
                "without noting the method change."
            )
            if not years:
                return errors.error_result(
                    errors.VINTAGE_UNAVAILABLE,
                    "Only 2020 was requested and the 2020 ACS 1-year release is unavailable "
                    f"({ACS1_EXPERIMENTAL_URL}).",
                    unavailable_years=[ACS1_EXPERIMENTAL_YEAR],
                )
        acs1_advisories.append(
            f"ACS 1-year: published only for areas with {ACS1_MIN_POPULATION:,}+ population "
            f"({ACS1_AREAS_URL}); 1-year periods do not overlap, so comparisons between releases "
            "use no overlap adjustment; never compare these with 5-year estimates "
            f"({COMPARING_ACS_URL})."
        )

    multi_year = len(years) > 1
    if options.harmonize_tracts_to is not None:
        if options.harmonize_tracts_to not in (2010, 2020):
            return errors.error_result(errors.INVALID_INPUT, "harmonize_tracts_to must be 2010 or 2020.")
        if geo_level != "tract":
            return errors.error_result(
                errors.INVALID_INPUT, "harmonize_tracts_to applies only to geo_level='tract'."
            )
        if output_format not in PANEL_FORMATS:
            return errors.error_result(
                errors.INVALID_OUTPUT_FORMAT,
                f"harmonize_tracts_to requires a panel output format {sorted(PANEL_FORMATS)}.",
            )

    if geo_level == BLOCK_GROUP and not decennial:
        if dataset != "acs5":
            return errors.error_result(
                errors.INVALID_GEO_LEVEL,
                f"Block groups are published only in the detailed tables (acs5), not '{dataset}'.",
            )
        early = [y for y in years if y < BLOCK_GROUP_FIRST_YEAR]
        if early:
            return errors.error_result(
                errors.INVALID_GEO_LEVEL,
                f"Block groups are in the ACS 5-year API from the {BLOCK_GROUP_FIRST_YEAR} release; "
                f"not available for {early}.",
            )

    conc = build_concordance(table_code, dataset, years, client)
    override_report = apply_overrides(conc, client)
    if not conc.concepts:
        return errors.error_result(
            errors.METADATA_UNAVAILABLE,
            f"No variable metadata found for {table_code} ({dataset}) in {years}.",
            issues=_issue_dicts(conc.issues, multi_year),
        )

    selected, unknown, resolution = conc.explain_selection(variables)
    if unknown:
        return errors.error_result(
            errors.INVALID_VARIABLES,
            f"Not found in {table_code} for {years[0]}-{years[-1]}: {unknown}",
            unknown_variables=unknown,
        )

    continuity_report: Dict[str, Any] = {}
    if multi_year and options.continuity_check and options.concordance_mode != "code":
        flags, continuity_report = continuity_check(conc, selected, client)
        conc.issues.extend(flags)

    relevant = conc.issues_for(selected)
    blocking = [i for i in relevant if _is_blocking(i, multi_year)]

    advisories: List[str] = []
    for adv in (
        (
            f"Tract panel will be harmonized to {options.harmonize_tracts_to} tracts with block-population "
            "weights (housing-unit weights for household and housing tables; land-area weights where block "
            "data cannot be fetched; census.geo_crosswalk / census.block_weights; first use downloads and "
            "caches the states' block files); see tract_harmonization in the extraction metadata. "
            "Apportioned MOEs assume independent components and exact weights."
            if options.harmonize_tracts_to and geo_level == "tract"
            else boundary_shift_advisory(years[0], years[-1], geo_level)
        ),
        home_value_advisory(table_code, years) if options.inflation_base_year else None,
        (
            provisional_advisory(options.price_index, list(years) + [options.inflation_base_year])
            if options.inflation_base_year and options.allow_provisional_index and options.cpi_table is None
            else None
        ),
    ):
        if adv:
            advisories.append(adv)
    if dataset == "cprofile":
        advisories.append(
            "Comparison Profiles carry several periods per release with Census-computed "
            "significance flags (*_SS); concepts are keyed by role (current, comparison, "
            "significance) because codes embed the period."
        )
    if variables is None and multi_year:
        advisories.append(
            "No variables= selection: every line of the table is checked, so unrelated "
            "drift can block the run. Name the variables your analysis uses."
        )

    advisories.extend(acs1_advisories)
    if decennial:
        advisories.append(DISCLOSURE_AVOIDANCE_ADVISORY)
        if geo_level == "block":
            advisories.append(BLOCK_ADVISORY)
        advisories.append(
            "Single census release: cross-release concordance and continuity checks do not apply; the "
            "concordance file only maps this release's codes to labels. Do not splice 2020 Census counts "
            "into an ACS series as if they were ACS estimates."
        )
        if options.compute_reliability:
            advisories.append(
                "compute_reliability: no SE, CV or reliability columns are added for 2020 Census counts "
                "(not sample-based; sampling MOE undefined, never 0)."
            )
    geography_availability: Dict[str, Any] = {}
    if product == PRODUCT_1YR:
        geography_availability = probe_acs1_geographies(client, dataset, years, entities)
        entities = geography_availability.pop("_entities")
        if geography_availability["unavailable"] and not any(
            e.applies(y) for e in entities for y in years
        ):
            return errors.error_result(
                errors.INVALID_GEO_LEVEL,
                "None of the requested areas is published in the ACS 1-year releases "
                f"{years} (areas of {ACS1_MIN_POPULATION:,}+ only; {ACS1_AREAS_URL}). "
                "Use the 5-year product.",
                geography_availability=geography_availability,
            )
        if geography_availability["unavailable"]:
            advisories.append(
                f"{len(geography_availability['unavailable'])} requested area(s) are not published "
                "in some or all 1-year releases (under 65,000 population); they are skipped for "
                "those years. See geography_availability."
            )

    block_group_availability: Dict[str, Any] = {}
    if geo_level == BLOCK_GROUP and not decennial:
        block_group_availability = probe_block_group_availability(client, table_code, years, entities)
        missing = block_group_availability["unpublished_years"]
        if missing:
            return errors.error_result(
                errors.INVALID_GEO_LEVEL,
                f"{table_code} is not published at block-group level for {missing} (the API returns "
                "only empty values); use tracts or a table published for block groups.",
                block_group_availability=block_group_availability,
            )
        advisories.append(
            "Block-group estimates carry large MOEs, and many detailed tables are not published for "
            "block groups (this table was probed: "
            + ("published in every checked year" if not block_group_availability["unchecked_years"]
               else f"unchecked for {block_group_availability['unchecked_years']}")
            + "). One API request per county per year."
        )

    would_block = False
    block_reason = ""
    if options.concordance_mode == "code" and multi_year and not options.allow_unverified_concordance:
        would_block, block_reason = True, errors.UNVERIFIED_CONCORDANCE
    elif options.concordance_mode == "fail" and blocking:
        would_block, block_reason = True, errors.CONCORDANCE_REVIEW_REQUIRED

    cache = client.cache
    calls = 0
    cached = 0
    for year in years:
        for ent in entities:
            if not ent.applies(year):
                continue
            calls += 1
            if cache is not None:
                try:
                    endpoint, params = client.build_request(
                        year=year, table_code=table_code, dataset=dataset, **ent.request_kwargs()
                    )
                except ValueError as exc:
                    return errors.error_result(errors.INVALID_GEO_LEVEL, str(exc))
                if cache.get(endpoint, params) is not None:
                    cached += 1

    issue_dicts = _issue_dicts(relevant, multi_year)
    issue_dicts.sort(key=lambda d: (not d["blocking"], d["kind"]))
    return {
        "success": True,
        "table_code": table_code,
        "dataset": dataset,
        "product": product,
        "years": years,
        "excluded_years": excluded_years,
        "geo_level": geo_level,
        "output_format": output_format,
        "concordance_mode": options.concordance_mode,
        "api_calls": calls,
        "cached_calls": cached,
        "request_targets": len(entities),
        "states_covered": _states_covered(entities),
        "block_group_availability": block_group_availability or None,
        "geography_availability": geography_availability or None,
        "selected_concepts": len(selected),
        "selection": resolution,
        "concordance_summary": conc.summary(),
        "issues": issue_dicts[:MAX_ISSUES_IN_RESULT],
        "issue_count": len(issue_dicts),
        "blocking_issue_count": sum(1 for d in issue_dicts if d["blocking"]),
        "would_block": would_block,
        "block_reason": block_reason,
        "advisories": advisories,
        "uncertainty": uncertainty_record(geo_level) if decennial else None,
        "errata": [] if decennial else _errata_summary(table_code, years),
        "overrides": override_report,
        "continuity": {
            "checked_concepts": len(continuity_report),
            "passed": sum(1 for r in continuity_report.values() if r["passed"]),
        },
        "_concordance": conc,
        "_entities": list(entities),
        "_options": options,
        "_selected": selected,
        "_continuity": continuity_report,
    }


def _entity_codes(ent: Entity) -> Tuple[str, Optional[List[str]]]:
    """(API key column, requested codes or None for a wildcard) for a 1-year probe."""
    level = normalize_geo_level(ent.geo_level)
    raw = {"county": ent.county_code, "place": ent.place_code, "msa": ent.msa_code}.get(level)
    key = GEO_LEVELS[level][0]
    if raw is None or str(raw).strip() in ("", "*"):
        return key, None
    return key, [c.strip() for c in str(raw).split(",") if c.strip()]


def probe_acs1_geographies(
    client: CensusAPIClient, dataset: str, years: Sequence[int], entities: Sequence[Entity]
) -> Dict[str, Any]:
    """Which requested areas each ACS 1-year release publishes (65,000+ population).

    One cached listing request (``get=NAME``) per year and state (counties,
    places) or per year (metropolitan areas); nation and states are always
    published. Wildcard requests ("every county") need no check: the API
    returns only published areas. Requested codes missing from a release's
    listing are reported under ``unavailable``; targets with no published code
    in a year get that year removed (``Entity.years``) in ``_entities``.
    Years whose listing fails (offline) are ``unchecked_years``.
    """
    listings: Dict[Tuple[int, str, str], Optional[Set[Tuple[str, str]]]] = {}
    unchecked: Set[int] = set()
    unavailable: List[Dict[str, Any]] = []
    out_entities: List[Entity] = []
    for ent in entities:
        level = normalize_geo_level(ent.geo_level)
        if level in ("us", "state"):
            out_entities.append(ent)
            continue
        key, codes = _entity_codes(ent)
        if codes is None:
            out_entities.append(ent)
            continue
        state = "" if level == "msa" else str(ent.state_code or "*")
        states = None if state in ("", "*") else set(state.split(","))
        keep: List[int] = []
        missing_by_year: Dict[str, List[str]] = {}
        applicable = [y for y in years if ent.applies(y)]
        for year in applicable:
            lk = (int(year), level, state)
            if lk not in listings:
                rows = client.list_geographies(year, level, state_code=state or None, dataset=dataset)
                listings[lk] = {(str(r.get("state", "")), str(r.get(key, ""))) for r in rows} if rows else None
            published = listings[lk]
            if published is None:
                unchecked.add(int(year))
                keep.append(year)
                continue
            found = {c for st, c in published if states is None or st in states}
            missing = [c for c in codes if c not in found]
            if missing:
                missing_by_year[str(year)] = missing
            if len(missing) < len(codes):
                keep.append(year)
        if missing_by_year:
            unavailable.append({
                "entity": ent.name, "geo_level": level, "state_code": ent.state_code,
                "missing_codes_by_year": missing_by_year,
            })
        out_entities.append(ent if keep == applicable else replace(ent, years=tuple(keep)))
    return {
        "min_population": ACS1_MIN_POPULATION,
        "source": ACS1_AREAS_URL,
        "unavailable": unavailable,
        "unchecked_years": sorted(unchecked),
        "_entities": out_entities,
    }


def probe_block_group_availability(
    client: CensusAPIClient, table_code: str, years: Sequence[int],
    entity: Union[Entity, Sequence[Entity]],
) -> Dict[str, Any]:
    """Is ``table_code`` published for block groups? One (cached) request per year.

    Requests the table for the first block-group target (one county). The API
    answers tables that are not published for block groups with rows of empty
    values, so a year is unpublished when every estimate is null. Years whose
    probe fails (e.g. offline) are listed under ``unchecked_years``.
    """
    targets = [entity] if isinstance(entity, Entity) else list(entity)
    unpublished: List[int] = []
    unchecked: List[int] = []
    for year in years:
        ent = next((e for e in targets if e.applies(year)), None)
        if ent is None:
            unchecked.append(year)
            continue
        endpoint, params = client.build_request(
            year=year, table_code=table_code, dataset="acs5", **ent.request_kwargs()
        )
        payload = client._get_json(endpoint, params)
        if not payload or len(payload) < 2:
            unchecked.append(year)
            continue
        header = [str(h) for h in payload[0]]
        est = [i for i, h in enumerate(header) if h.startswith(table_code + "_") and h.endswith("E")]
        if est and all(row[i] in (None, "") for row in payload[1:] for i in est):
            unpublished.append(year)
    return {"probed_entity": targets[0].name if targets else "",
            "unpublished_years": unpublished, "unchecked_years": unchecked}


def _states_covered(entities: Sequence[Entity]) -> int:
    """Distinct states the request targets cover (0 for national-only levels)."""
    states: Set[str] = set()
    for ent in entities:
        if ent.state_code and ent.geo_level not in ("us", "msa", "zcta"):
            states.update(c.strip() for c in ent.state_code.split(",") if c.strip())
    return len(states)


def public_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop private keys so a plan can be serialized."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _git_commit() -> Optional[str]:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _vintage_provenance(conc: Concordance) -> Dict[str, Any]:
    files = load_manifest().get("files", {})
    out: Dict[str, Any] = {}
    for year, source in conc.sources.items():
        entry = files.get(f"{conc.dataset}/{year}/variables.json.gz", {})
        out[str(year)] = {"source": source, "sha256": entry.get("sha256"), "url": entry.get("url")}
    return out


def file_sha256(path: str) -> str:
    """SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_file_hashes(output_dir: str, paths: Sequence[Optional[str]], folders: Sequence[Optional[str]]) -> Dict[str, str]:
    """Map output paths (relative to output_dir, '/'-separated) to SHA-256."""
    found: List[str] = [p for p in paths if p and os.path.isfile(p)]
    for folder in folders:
        if folder and os.path.isdir(folder):
            for root, _dirs, names in os.walk(folder):
                found.extend(os.path.join(root, n) for n in names)
    out: Dict[str, str] = {}
    for path in sorted(set(found)):
        rel = os.path.relpath(path, output_dir).replace(os.sep, "/")
        out[rel] = file_sha256(path)
    return out


def _replay_command(metadata_path: str) -> str:
    return f'python -m census replay "{os.path.abspath(metadata_path)}" --json'


def _write_json(path: str, payload: Any) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_batch(
    table_code: str,
    entities: Sequence[Entity],
    years: Sequence[int],
    output_dir: str,
    output_format: str = "both",
    options: Optional[ResearchOptions] = None,
    dataset: Optional[str] = None,
    client: Optional[CensusAPIClient] = None,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    variables: Optional[Sequence[str]] = None,
    plan: Optional[Mapping[str, Any]] = None,
    product: Optional[str] = None,
) -> Dict[str, Any]:
    """Plan, then download ``table_code`` for every entity x year and write outputs.

    Never raises on API failures; check ``success``, ``error_code`` and
    ``failed_calls``. When the plan blocks, the concordance and issue files are
    still written to ``output_dir`` for review. ``plan`` reuses the result of
    :func:`plan_extraction` for the same arguments (skips planning again:
    concordance build and block-group probes run once).
    """
    options = options or ResearchOptions()
    emit: LogFn = log or (lambda _msg: None)
    table_code = table_code.strip().upper()
    if client is None:
        client = CensusAPIClient(cache=get_default_cache() if options.use_cache else None)

    if plan is None:
        plan = plan_extraction(
            table_code, entities, years, output_format, options, dataset, variables, client,
            product=product,
        )
    if not plan.get("success"):
        return plan
    years = plan["years"]
    dataset = plan["dataset"]
    entities = plan.get("_entities") or entities
    options = plan.get("_options") or options
    conc: Concordance = plan["_concordance"]
    selected: Set[str] = plan["_selected"]
    multi_year = len(years) > 1
    geo_level = plan["geo_level"]
    span = f"{years[0]}-{years[-1]}" if multi_year else str(years[0])

    os.makedirs(output_dir, exist_ok=True)
    for adv in plan["advisories"]:
        emit(adv)

    use_concordance = options.concordance_mode != "code"
    reps = conc.representative_bases()
    if use_concordance and len(set(reps.values())) != len(reps):
        raise AssertionError("Two concepts share a panel column; concordance is inconsistent.")
    slugs_by_concept: Dict[str, str] = {}

    concordance_path = os.path.join(output_dir, f"{table_code}_concordance.csv")
    issues_path = os.path.join(output_dir, f"{table_code}_concordance_issues.json")
    all_issues = _issue_dicts(conc.issues_for(selected), multi_year)
    _write_json(
        issues_path,
        {
            "table_code": table_code,
            "dataset": dataset,
            "years": years,
            "summary": conc.summary(),
            "continuity": plan["_continuity"],
            "issues": all_issues,
        },
    )

    if plan["would_block"]:
        conc.write_csv(concordance_path, selected=selected)
        message = (
            f"{plan['blocking_issue_count']} blocking concordance issue(s) affect the selected "
            f"variables of {table_code} {span}. Review {issues_path}, resolve them with reviewed "
            "overrides (see docs/agent_protocol.md), narrow variables=, or use "
            "concordance_mode='strict' for exploratory output."
            if plan["block_reason"] == errors.CONCORDANCE_REVIEW_REQUIRED
            else "concordance_mode='code' across several years pools codes without checking "
            "their meaning; pass allow_unverified_concordance=True to accept that."
        )
        return errors.error_result(
            plan["block_reason"],
            message,
            plan=public_plan(plan),
            concordance_path=concordance_path,
            issues_path=issues_path,
        )

    is_panel = output_format in PANEL_FORMATS
    total_calls = sum(1 for y in years for e in entities if e.applies(y))
    call_index = 0
    successful_calls = 0
    failed_calls: List[Dict[str, Any]] = []
    seen_columns: Dict[str, None] = {}
    panel_frames: List[pd.DataFrame] = []

    rep_metadata = conc.representative_metadata()
    groups: Dict[str, List[Entity]] = {}
    for ent in entities:
        groups.setdefault(ent.group, []).append(ent)

    emit(
        f"Starting download: {len(entities)} request target(s) x {len(years)} year(s) "
        f"({total_calls} calls, {plan['cached_calls']} cached). Table {table_code} "
        f"[{dataset}], format: {output_format}, concordance policy: {options.concordance_mode}."
    )

    saved_entities: List[Dict[str, Any]] = []
    for group_idx, (group_name, members) in enumerate(groups.items(), 1):
        folder = os.path.join(output_dir, f"{sanitize_filename(group_name)}_{table_code}")
        if not is_panel:
            os.makedirs(folder, exist_ok=True)
        acc: Dict[str, List[pd.DataFrame]] = {"wide": [], "melted": []}
        years_saved: List[int] = []
        rows_saved = 0
        emit(f"[{group_idx}/{len(groups)}] Processing {group_name} ({geo_level})...")

        for year in years:
            year_ok = False
            year_meta = conc.year_metadata.get(year) or resolve_metadata(table_code, dataset, year, client)
            code_map = conc.code_map(year)
            mapping = {
                base: rep for base, rep in conc.rename_map(year).items() if code_map[base] in selected
            }
            for ent in members:
                if not ent.applies(year):
                    continue
                call_index += 1
                if progress:
                    progress(call_index, total_calls, ent.name, year)
                data = client.fetch(
                    year=year, table_code=table_code, dataset=dataset, **ent.request_kwargs()
                )
                if not data or len(data) < 2:
                    reason = client.last_error or "no data returned"
                    failed_calls.append({"entity": ent.name, "year": year, "error": reason})
                    emit(f"  {year} {ent.name}: skipped ({reason}).")
                    continue

                coded = prepare_frame(data, year, options, year_meta, geo_level)
                if use_concordance:
                    coded = apply_concordance(coded, mapping)
                for col in coded.columns:
                    seen_columns.setdefault(str(col), None)
                successful_calls += 1
                rows_saved += len(coded)
                year_ok = True

                if is_panel:
                    coded = coded.copy()
                    coded.insert(1, "Year", year)
                    panel_frames.append(coded)
                else:
                    labels_from = rep_metadata if use_concordance else year_meta
                    rename = {
                        k: v
                        for k, v in describe_columns(coded.columns, labels_from).items()
                        if k not in IDENTIFIER_COLUMNS
                    }
                    process_data_and_save(
                        formatted_df=coded.rename(columns=rename),
                        folder_name=folder,
                        entity_name=ent.name,
                        table_code=table_code,
                        year=year,
                        output_format=output_format,
                        excel_accumulator=acc,
                    )
                emit(f"  {year} {ent.name}: {len(coded)} row(s), {len(coded.columns)} columns.")
            if year_ok:
                years_saved.append(year)

        excel_path = None
        if output_format == "excel":
            excel_path = save_excel_workbook(folder, group_name, table_code, acc)
            if excel_path:
                emit(f"  Excel workbook saved: {os.path.basename(excel_path)}")
        saved_entities.append(
            {
                "entity": group_name,
                "folder": None if is_panel else folder,
                "years_saved": years_saved,
                "rows_saved": rows_saved,
                "excel_path": excel_path,
            }
        )

    if use_concordance:
        label_metadata: Dict[str, Dict[str, str]] = rep_metadata
    else:
        label_metadata = {}
        for year in years:  # latest vintage wins
            label_metadata.update(conc.year_metadata.get(year, {}))

    panel_path: Optional[str] = None
    harmonization: Optional[Dict[str, Any]] = None
    duplicates_dropped = 0
    slug_map: Optional[Dict[str, str]] = None
    columns_out: List[str] = list(seen_columns)

    if is_panel and panel_frames:
        panel, duplicates_dropped = build_tidy_panel(panel_frames)
        if options.harmonize_tracts_to and geo_level == "tract":
            from census.geo_crosswalk import harmonize_panel

            try:
                panel, harmonization = harmonize_panel(
                    panel,
                    target_vintage=options.harmonize_tracts_to,
                    metadata=label_metadata,
                    return_record=True,
                )
            except ValueError as exc:
                return errors.error_result(
                    errors.INVALID_INPUT,
                    f"Tract harmonization refused: {exc}",
                    output_dir=output_dir,
                )
            emit(f"Harmonized tract panel to {options.harmonize_tracts_to} tracts.")
        columns_out = list(panel.columns)
        slug_map = make_slug_map(columns_out, label_metadata, options.slug_style)
        panel = panel.rename(columns=slug_map)
        if is_decennial(dataset):  # explicit evidence for census.analysis (kept in Parquet files)
            panel.attrs.update(product=PRODUCT_DECENNIAL, dataset=dataset, variable_codes={
                slug: col for col, slug in slug_map.items()
                if (parse_variable_code(col) or ("", ""))[1] == "N"})
        if duplicates_dropped:
            emit(f"Dropped {duplicates_dropped} duplicate (GEOID, Year) row(s).")
        ext = "parquet" if output_format == "parquet" else "csv"
        panel_path = os.path.join(output_dir, f"{table_code}_{geo_level}_panel_{span}.{ext}")
        if ext == "parquet":
            export_parquet(panel, panel_path)
        else:
            panel.to_csv(panel_path, index=False, encoding="utf-8")
        emit(f"Tidy panel saved: {panel_path} ({len(panel)} rows)")
    elif not is_panel and "Year" not in seen_columns:
        columns_out = ["Year"] + columns_out

    if slug_map and use_concordance:
        base_to_cid = {b: cid for cid, b in reps.items()}
        for col, slug in slug_map.items():
            parsed = parse_variable_code(col)
            if parsed and parsed[1] in ("E", "PE", "SS") and parsed[0] in base_to_cid:
                slugs_by_concept.setdefault(base_to_cid[parsed[0]], slug)
    if use_concordance:
        conc.write_csv(concordance_path, slugs_by_concept, selected)
    else:
        concordance_path = None  # type: ignore[assignment]

    universe = table_universe(table_code, dataset, years[-1], client)
    codebook_path: Optional[str] = None
    if options.include_codebook and successful_calls:
        codebook = generate_codebook(
            columns_out,
            label_metadata,
            universe=universe,
            slug_map=slug_map,
            inflation_base_year=options.inflation_base_year,
            slug_style=options.slug_style,
            price_index=INDEX_DISPLAY.get(options.price_index, options.price_index),
            concordance=conc if use_concordance else None,
            decennial=is_decennial(dataset),
        )
        codebook_path = os.path.join(output_dir, f"{table_code}_codebook.csv")
        codebook.to_csv(codebook_path, index=False, encoding="utf-8")
        emit(f"Codebook saved: {codebook_path} ({len(codebook)} variables)")

    from census.manifest import data_source_record

    overrides_file = crosswalk_path(dataset, table_code) if conc.overrides_applied else None
    metadata_path = os.path.join(output_dir, f"{table_code}_extraction_metadata.json")
    metadata_doc = {
        "tract_harmonization": harmonization,
        "replay_schema_version": REPLAY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_version": PACKAGE_VERSION,
        "git_commit": _git_commit(),
        "table_code": table_code,
        "dataset": dataset,
        "product": plan.get("product", PRODUCT_5YR),
        "excluded_years": plan.get("excluded_years") or [],
        "geography_availability": plan.get("geography_availability"),
        "endpoint": client.endpoint(years[0], dataset).replace(str(years[0]), "{year}", 1),
        # observed at extraction time: a cache marked synthetic labels the extraction synthetic
        "data_source": data_source_record(client),
        "years": years,
        "geo_level": geo_level,
        "entities": [asdict(e) for e in entities],
        "output_format": output_format,
        "variables_requested": list(variables) if variables else None,
        "variables": list(variables) if variables else None,
        "selected_concept_ids": sorted(selected),
        "options": {k: v for k, v in asdict(options).items() if k != "cpi_table"},
        # A caller-supplied price index table, so replay deflates identically.
        "cpi_table": (
            {str(y): v for y, v in sorted(options.cpi_table.items())}
            if options.cpi_table is not None
            else None
        ),
        "inflation": (
            {
                "base_year": options.inflation_base_year,
                "index": INDEX_DISPLAY.get(options.price_index, options.price_index),
                "source": price_index_info(options.price_index) if options.cpi_table is None else "cpi_table supplied by caller",
            }
            if options.inflation_base_year
            else None
        ),
        "concordance": {
            "mode": options.concordance_mode,
            "normalization_version": NORMALIZATION_VERSION,
            "summary": conc.summary(),
            "selected_concepts": len(selected),
            "overrides_file": overrides_file,
            "overrides_sha256": (
                file_sha256(overrides_file)
                if overrides_file and os.path.isfile(overrides_file)
                else None
            ),
            "overrides_applied": conc.overrides_applied,
            "rules_applied": getattr(conc, "rules_applied", []),
            "metadata_vintages": _vintage_provenance(conc),
        },
        # 2020 Census counts are not sample-based: no MOE confidence level (never 0 MOEs).
        "moe_confidence_level": None if is_decennial(dataset) else 0.90,
        "uncertainty": plan.get("uncertainty"),
        "advisories": plan["advisories"],
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "outputs": {
            "panel": panel_path,
            "codebook": codebook_path,
            "concordance": concordance_path,
            "issues": issues_path,
        },
        # Every file this run wrote (except this metadata file) -> SHA-256,
        # keyed by path relative to the output directory; used by replay.
        "output_files": _output_file_hashes(
            output_dir,
            [panel_path, codebook_path, concordance_path, issues_path],
            [e["folder"] for e in saved_entities],
        ),
        "replay_command": _replay_command(metadata_path),
    }
    _write_json(metadata_path, metadata_doc)

    return {
        "success": True,
        "base_directory": output_dir,
        "table_code": table_code,
        "dataset": dataset,
        "total_calls_attempted": total_calls,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "format": output_format,
        "entities": saved_entities,
        "panel_path": panel_path,
        "codebook_path": codebook_path,
        "concordance_path": concordance_path,
        "issues_path": issues_path,
        "metadata_path": metadata_path,
        "duplicates_dropped": duplicates_dropped,
        "cached_calls": plan["cached_calls"],
        "resume_hint": (
            None
            if not failed_calls
            else "Re-run the same extraction to retry failed_calls; successful requests are "
            "served from the query cache."
            if client.cache is not None
            else "Re-run with use_cache=True so a retry only re-requests failed_calls."
        ),
        "replay_command": metadata_doc["replay_command"],
        "advisories": plan["advisories"],
        "concordance_issues": all_issues[:MAX_ISSUES_IN_RESULT],
        "concordance_issue_count": len(all_issues),
    }


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

DIFFABLE_EXTENSIONS = (".csv", ".parquet", ".xlsx")
MAX_DIFF_SAMPLES = 20

REVISION_EXPLANATION = (
    "Replay outputs differ from the original. The Census Bureau occasionally revises "
    "published ACS values and geography names, and the package or metadata archive may "
    "have changed (see provenance_differences); the changes are listed cell by cell so "
    "they can be judged, not treated as an error."
)


def _load_table(path: str) -> Dict[str, pd.DataFrame]:
    """Read an output as all-string frame(s) so comparison is exact."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return {"": pd.read_csv(path, dtype=str, keep_default_na=False)}
    if ext == ".parquet":
        df = pd.read_parquet(path)
        return {"": df.astype(object).where(df.notna(), "").astype(str)}
    sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False)
    return {str(k): v for k, v in sheets.items()}


def _key_columns(old: pd.DataFrame, new: pd.DataFrame) -> List[str]:
    keys = [c for c in old.columns if str(c).lower() in ("geoid", "year") and c in new.columns]
    if not keys or old.duplicated(keys).any() or new.duplicated(keys).any():
        return []
    return keys


def _row_ids(frame: pd.DataFrame, row: Any, keys: List[str]) -> Dict[str, Any]:
    if keys:
        values = row if isinstance(row, tuple) else (row,)
        ids = dict(zip(keys, values))
    else:
        ids = {"row": int(row)}
        for col in frame.columns:
            if str(col).lower() in ("geoid", "year"):
                ids[col] = frame.at[row, col]
    out: Dict[str, Any] = {}
    for k, v in ids.items():
        lk = str(k).lower()
        out["GEOID" if lk == "geoid" else "year" if lk == "year" else k] = v
    return out


def diff_frames(
    old: pd.DataFrame, new: pd.DataFrame, max_samples: int = MAX_DIFF_SAMPLES
) -> Dict[str, Any]:
    """Cell-level comparison of two output tables (values compared as strings).

    Rows are matched on (GEOID, Year) when those identify rows uniquely,
    otherwise by position.
    """
    keys = _key_columns(old, new)
    o = old.set_index(keys) if keys else old.reset_index(drop=True)
    n = new.set_index(keys) if keys else new.reset_index(drop=True)
    cols = [c for c in o.columns if c in n.columns]
    rows = o.index.intersection(n.index)
    oc = o.loc[rows, cols].astype(str)
    nc = n.loc[rows, cols].astype(str)
    changed = oc.ne(nc)
    n_changed = int(changed.values.sum())
    samples: List[Dict[str, Any]] = []
    if n_changed:
        positions = list(zip(*changed.values.nonzero()))[:max_samples]
        source = o if keys else old.reset_index(drop=True)
        for r, c in positions:
            sample = _row_ids(source, changed.index[r], keys)
            sample.update({"column": str(cols[c]), "old": oc.iat[r, c], "new": nc.iat[r, c]})
            samples.append(sample)
    by_column = changed.sum()
    removed = o.index.difference(n.index)
    added = n.index.difference(o.index)

    def _labels(index: Any) -> List[Any]:
        return [list(x) if isinstance(x, tuple) else x for x in list(index[:max_samples])]

    return {
        "matched_on": keys or "row_position",
        "n_cells_compared": int(changed.size),
        "n_cells_changed": n_changed,
        "changed_by_column": {str(k): int(v) for k, v in by_column[by_column > 0].items()},
        "changes_sample": samples,
        "rows_removed": int(len(removed)),
        "rows_added": int(len(added)),
        "rows_removed_sample": _labels(removed),
        "rows_added_sample": _labels(added),
        "columns_removed": [str(c) for c in o.columns if c not in n.columns],
        "columns_added": [str(c) for c in n.columns if c not in o.columns],
    }


def _diff_files(old_path: str, new_path: str) -> Dict[str, Any]:
    old_sheets, new_sheets = _load_table(old_path), _load_table(new_path)
    sheets: Dict[str, Any] = {}
    for name in sorted(set(old_sheets) | set(new_sheets)):
        if name not in old_sheets or name not in new_sheets:
            sheets[name] = {"sheet_added" if name in new_sheets else "sheet_removed": True}
            continue
        sheets[name] = diff_frames(old_sheets[name], new_sheets[name])
    if list(sheets) == [""]:
        return sheets[""]
    return {
        "n_cells_changed": sum(d.get("n_cells_changed", 0) for d in sheets.values()),
        "sheets": sheets,
    }


_DIFF_FLAGS = (
    "n_cells_changed", "rows_removed", "rows_added", "columns_removed",
    "columns_added", "sheet_added", "sheet_removed",
)


def _data_identical(diff: Mapping[str, Any]) -> bool:
    parts = list(diff["sheets"].values()) if "sheets" in diff else [diff]
    return all(not any(p.get(k) for k in _DIFF_FLAGS) for p in parts)


def _verify_outputs(
    original: Mapping[str, Any], replay: Mapping[str, Any], original_dir: str, replay_dir: str
) -> Dict[str, Any]:
    old_files: Mapping[str, str] = original.get("output_files") or {}
    new_files: Mapping[str, str] = replay.get("output_files") or {}
    files: Dict[str, Dict[str, Any]] = {}
    for rel in sorted(set(old_files) | set(new_files)):
        entry: Dict[str, Any] = {
            "original_sha256": old_files.get(rel),
            "replay_sha256": new_files.get(rel),
        }
        if rel not in new_files:
            entry["status"] = "missing_in_replay"
        elif rel not in old_files:
            entry["status"] = "new_in_replay"
        elif old_files[rel] == new_files[rel]:
            entry["status"] = "identical"
        else:
            entry["status"] = "changed"
            old_path = os.path.join(original_dir, *rel.split("/"))
            new_path = os.path.join(replay_dir, *rel.split("/"))
            if not rel.lower().endswith(DIFFABLE_EXTENSIONS):
                entry["diff"] = None
            elif not os.path.isfile(old_path):
                entry["diff_unavailable"] = "original file no longer exists; hashes only"
            elif file_sha256(old_path) != old_files[rel]:
                entry["diff_unavailable"] = "original file was modified after extraction"
            else:
                try:
                    entry["diff"] = _diff_files(old_path, new_path)
                except Exception as exc:  # unreadable file: report, do not fail the replay
                    entry["diff_unavailable"] = f"could not compare: {exc}"
                else:
                    if _data_identical(entry["diff"]):
                        # Same cells, different bytes (e.g. workbook timestamps).
                        entry["status"] = "equivalent"
        files[rel] = entry
    counts: Dict[str, int] = {}
    for entry in files.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    verified = bool(files) and all(
        e["status"] in ("identical", "equivalent") for e in files.values()
    )
    if not old_files:
        explanation = "The original metadata records no output files; nothing to verify."
        verified = False
    elif verified:
        explanation = "Every output file matches the original (byte-identical or identical cells)."
    else:
        explanation = REVISION_EXPLANATION
    return {
        "verified": verified,
        "file_status_counts": counts,
        "n_cells_changed": sum(
            (e.get("diff") or {}).get("n_cells_changed", 0) for e in files.values()
        ),
        "files": files,
        "explanation": explanation,
    }


def _provenance_differences(original: Mapping[str, Any], replay: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    if original.get("package_version") != replay.get("package_version"):
        out.append(
            f"package_version {original.get('package_version')} -> {replay.get('package_version')}"
        )
    old_v = (original.get("concordance") or {}).get("metadata_vintages") or {}
    new_v = (replay.get("concordance") or {}).get("metadata_vintages") or {}
    for year in sorted(set(old_v) | set(new_v)):
        a, b = old_v.get(year) or {}, new_v.get(year) or {}
        if a.get("sha256") != b.get("sha256") or a.get("source") != b.get("source"):
            out.append(
                f"{year} metadata: {a.get('source')}/{a.get('sha256')} -> "
                f"{b.get('source')}/{b.get('sha256')}"
            )
    a_c, b_c = original.get("concordance") or {}, replay.get("concordance") or {}
    if a_c.get("overrides_sha256") != b_c.get("overrides_sha256"):
        out.append(
            f"overrides file {a_c.get('overrides_sha256')} -> {b_c.get('overrides_sha256')}"
        )
    if original.get("selected_concept_ids") != replay.get("selected_concept_ids"):
        out.append("variables resolved to a different concept selection")
    return out


def _entity_from_dict(d: Mapping[str, Any]) -> Entity:
    names = {f.name for f in fields(Entity)}
    return Entity(**{k: v for k, v in d.items() if k in names})


def _options_from_metadata(meta: Mapping[str, Any]) -> ResearchOptions:
    names = {f.name for f in fields(ResearchOptions)}
    recorded = meta.get("options") or {}
    opts = ResearchOptions(**{k: v for k, v in recorded.items() if k in names and k != "cpi_table"})
    if meta.get("cpi_table"):
        opts.cpi_table = {int(y): float(v) for y, v in meta["cpi_table"].items()}
    return opts


def replay_extraction(
    metadata_path: str,
    output_dir: Optional[str] = None,
    verify: bool = True,
    client: Optional[CensusAPIClient] = None,
    refresh: bool = False,
    log: Optional[LogFn] = None,
) -> Dict[str, Any]:
    """Re-run an extraction from its ``<table>_extraction_metadata.json``.

    Uses the recorded table, dataset, years, exact entity list, variables and
    research options (including a caller-supplied cpi_table). Outputs go to
    ``output_dir`` (default ``<original dir>/replays/<UTC timestamp>``; the
    original directory itself is refused so the files being verified survive).

    With ``verify``, every output file's SHA-256 is compared with the hash in
    the original metadata, and changed CSV/Parquet/Excel outputs are compared
    cell by cell (GEOID/year/column/old/new). Differences, e.g. Census
    revisions, give ``success: True, verified: False`` with an explanation.

    ``refresh=True`` bypasses the query cache so the live API is queried
    (detects revisions); by default the recorded ``use_cache`` option is
    honoured, which reproduces cached responses offline.
    """
    try:
        with open(metadata_path, encoding="utf-8") as f:
            original = json.load(f)
    except OSError as exc:
        return errors.error_result(
            errors.INVALID_INPUT, f"Cannot read extraction metadata '{metadata_path}': {exc}"
        )
    except ValueError as exc:
        return errors.error_result(errors.INVALID_INPUT, f"'{metadata_path}' is not valid JSON: {exc}")
    if not isinstance(original, dict):
        return errors.error_result(
            errors.INVALID_INPUT, f"'{metadata_path}' is not extraction metadata."
        )
    missing = [k for k in REPLAY_REQUIRED_FIELDS if k not in original]
    if missing:
        return errors.error_result(
            errors.INVALID_INPUT,
            f"'{metadata_path}' (package_version {original.get('package_version', 'unknown')}) "
            f"lacks fields needed for replay: {missing}. Metadata written before replay "
            f"support (schema {REPLAY_SCHEMA_VERSION}) cannot be replayed exactly; re-run the "
            "extraction with the current package to get replayable metadata.",
            missing_fields=missing,
            metadata_package_version=original.get("package_version"),
        )
    try:
        schema = int(original["replay_schema_version"])
    except (TypeError, ValueError):
        schema = REPLAY_SCHEMA_VERSION + 1
    if schema > REPLAY_SCHEMA_VERSION:
        return errors.error_result(
            errors.INVALID_INPUT,
            f"Metadata replay schema {original['replay_schema_version']!r} is not supported "
            f"by this package (supports {REPLAY_SCHEMA_VERSION}); upgrade the package.",
        )

    original_dir = os.path.dirname(os.path.abspath(metadata_path))
    if output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output_dir = os.path.join(original_dir, "replays", stamp)
    if os.path.normcase(os.path.abspath(output_dir)) == os.path.normcase(original_dir):
        return errors.error_result(
            errors.INVALID_INPUT,
            "output_dir is the original extraction directory; replaying there would "
            "overwrite the outputs being verified. Choose another directory.",
        )

    try:
        entities = [_entity_from_dict(d) for d in original["entities"]]
        options = _options_from_metadata(original)
    except (TypeError, ValueError, AttributeError) as exc:
        return errors.error_result(errors.INVALID_INPUT, f"Malformed metadata: {exc}")
    if refresh:
        options.use_cache = False
    if client is None:
        client = CensusAPIClient(cache=get_default_cache() if options.use_cache else None)

    result = run_batch(
        original["table_code"],
        entities,
        original["years"],
        output_dir,
        original["output_format"],
        options,
        original["dataset"],
        client,
        log=log,
        variables=original["variables"],
    )
    base = {
        "output_dir": output_dir,
        "original_metadata_path": os.path.abspath(metadata_path),
        "data_source": "query cache allowed" if client.cache is not None else "live API",
    }
    if not result.get("success"):
        return {**result, **base}

    with open(result["metadata_path"], encoding="utf-8") as f:
        replayed = json.load(f)
    out: Dict[str, Any] = {
        "success": True,
        **base,
        "metadata_path": result["metadata_path"],
        "table_code": result["table_code"],
        "successful_calls": result["successful_calls"],
        "failed_calls": result["failed_calls"],
        "provenance_differences": _provenance_differences(original, replayed),
    }
    if verify:
        out.update(_verify_outputs(original, replayed, original_dir, output_dir))
        if result["failed_calls"]:
            out["explanation"] += (
                f" {len(result['failed_calls'])} request(s) failed during replay "
                "(see failed_calls), so missing rows may reflect API errors, not revisions."
            )
    else:
        out["verified"] = None
    return out
