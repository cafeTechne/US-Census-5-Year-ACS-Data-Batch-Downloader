"""US Census ACS Batch Downloader — MCP Server for Claude Code & Claude Desktop.

Exposes Census data query, table exploration, batch download, and research
statistics tools (MOE/CV/significance, CPI-U deflation, codebooks) over the
Model Context Protocol (MCP).
"""

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
import pandas as pd

from census import (
    CPI_U_ANNUAL,
    DATASET_TABLES,
    STATE_MAPPING,
    CensusAPIClient,
    ResearchOptions,
    add_geoid_column,
    add_reliability_columns,
    build_entities,
    clean_census_sentinels,
    counties_for_state,
    get_default_cache,
    get_local_places_file,
    get_resource_path,
    get_variable_descriptions,
    infer_dataset,
    load_places_from_local,
    load_table_universes,
    load_tables_from_local,
    load_variable_definitions,
    resolve_metadata,
    resolve_zip,
    run_batch,
    z_test,
    zctas_for_state,
)
from census import adjust_inflation as _adjust_inflation
from census import calculate_cv as _calculate_cv
from census import generate_codebook as _generate_codebook
from census.panel import IDENTIFIER_COLUMNS, describe_columns
from census.stats import acs5_overlap_years

load_dotenv()

# Initialize FastMCP Server
mcp = FastMCP("US-Census-ACS-Downloader")

DEFAULT_YEAR = 2022


def _client(use_cache: bool = True) -> CensusAPIClient:
    return CensusAPIClient(cache=get_default_cache() if use_cache else None)


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_states() -> List[Dict[str, str]]:
    """List all supported US states and territories with their 2-digit FIPS codes."""
    return [
        {"state_code": code, "state_name": name}
        for code, name in sorted(STATE_MAPPING.items(), key=lambda x: x[1])
    ]


@mcp.tool()
def list_places(
    state_code: str, search: str = "", limit: int = 100
) -> List[Dict[str, str]]:
    """List incorporated places, cities, towns, and CDPs for a state FIPS code.

    Args:
        state_code: 2-digit state FIPS code (e.g. '36' for New York, '48' for Texas).
        search: Optional substring query to filter place names (case-insensitive).
        limit: Maximum number of places to return (default: 100).
    """
    places_file = get_local_places_file(state_code)
    if not places_file or not os.path.exists(places_file):
        return []

    raw_places = load_places_from_local(places_file)
    results: List[Dict[str, str]] = []
    query = search.strip().lower()

    for p in raw_places:
        # tuple: (state_code, place_code, place_name, place_type, counties)
        if not query or query in p[2].lower():
            results.append(
                {
                    "state_code": p[0],
                    "place_code": p[1],
                    "place_name": p[2],
                    "place_type": p[3],
                    "counties": p[4],
                    "geoid": f"{p[0]}{p[1]}",
                }
            )
            if len(results) >= limit:
                break

    return results


@mcp.tool()
def list_counties(state_code: str, year: int = DEFAULT_YEAR) -> List[Dict[str, str]]:
    """List counties (name + 3-digit FIPS + 5-digit GEOID) in a state.

    Uses the Census API; falls back to county codes from zip2fips.json offline.
    """
    rows = _client().list_geographies(year, "county", state_code=state_code)
    if rows:
        return sorted(
            (
                {
                    "county_code": r.get("county", ""),
                    "county_name": r.get("NAME", ""),
                    "geoid": f"{r.get('state', '')}{r.get('county', '')}",
                }
                for r in rows
            ),
            key=lambda r: r["county_code"],
        )
    return [
        {"county_code": c, "county_name": "", "geoid": f"{state_code}{c}"}
        for c in counties_for_state(state_code)
    ]


@mcp.tool()
def list_tracts(
    state_code: str, county_code: str, year: int = DEFAULT_YEAR
) -> Dict[str, Any]:
    """List all Census tracts (summary level 140) in a county with 11-digit GEOIDs.

    Args:
        state_code: 2-digit state FIPS (e.g. '36').
        county_code: 3-digit county FIPS (e.g. '061' for New York County).
        year: ACS 5-year vintage, which determines tract boundaries (2020+
            uses 2020 Census tracts; 2019 and earlier use 2010 tracts).
    """
    client = _client()
    rows = client.list_geographies(
        year, "tract", state_code=state_code, county_code=county_code
    )
    if not rows:
        return {
            "success": False,
            "error": client.last_error or "No tracts returned.",
            "tracts": [],
        }
    tracts = sorted(
        (
            {
                "tract_code": r.get("tract", ""),
                "name": r.get("NAME", ""),
                "geoid": f"{r.get('state', '')}{r.get('county', '')}{r.get('tract', '')}",
            }
            for r in rows
        ),
        key=lambda r: r["geoid"],
    )
    return {
        "success": True,
        "state_code": state_code,
        "county_code": county_code,
        "year": year,
        "tract_count": len(tracts),
        "tracts": tracts,
    }


@mcp.tool()
def list_zctas(
    state_code: Optional[str] = None,
    county_code: Optional[str] = None,
    zip_code: Optional[str] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """List ZIP Code Tabulation Areas in a state/county, or resolve a ZIP code.

    Uses the bundled zip2fips.json crosswalk (ZIP -> primary county).

    Args:
        state_code: 2-digit state FIPS (e.g. '48').
        county_code: Optional 3-digit county FIPS to narrow results.
        zip_code: Resolve a single 5-digit ZIP to its state/county instead.
        limit: Maximum ZCTAs returned (default 500).
    """
    if zip_code:
        resolved = resolve_zip(zip_code)
        if not resolved:
            return {"success": False, "error": f"ZIP code {zip_code} not found."}
        return {"success": True, "resolved": resolved}
    if not state_code:
        return {"success": False, "error": "Provide state_code or zip_code."}
    zctas = zctas_for_state(state_code, county_code)
    return {
        "success": True,
        "state_code": state_code,
        "county_code": county_code,
        "zcta_count": len(zctas),
        "zctas": zctas[:limit],
        "has_more": len(zctas) > limit,
        "note": "ZIP codes are USPS routes; ZCTAs approximate them. PO-box-only "
        "ZIPs have no ZCTA and return no Census data.",
    }


@mcp.tool()
def search_tables(query: str, limit: int = 25) -> List[Dict[str, str]]:
    """Search Census ACS 5-year table groups by code or keyword.

    Includes detailed tables plus curated Data Profiles (DP02-DP05), Subject
    tables (S....) and Comparison Profiles (CP02-CP05).

    Args:
        query: Table code (e.g. 'B01001', 'DP03', 'S1701') or keyword (e.g. 'poverty').
        limit: Maximum number of matching tables to return (default: 25).
    """
    groups_file = get_resource_path("groups.json")
    all_tables: Dict[str, str] = {}
    if os.path.exists(groups_file):
        all_tables = load_tables_from_local(groups_file)
    for curated in DATASET_TABLES.values():
        all_tables.update(curated)

    q = query.strip().lower()
    matches: List[Dict[str, str]] = []

    for code, desc in sorted(all_tables.items()):
        if q in code.lower() or q in desc.lower():
            matches.append(
                {"table_code": code, "description": desc, "dataset": infer_dataset(code)}
            )
            if len(matches) >= limit:
                break

    return matches


@mcp.tool()
def list_variables(table_code: str, limit: int = 50) -> Dict[str, Any]:
    """Retrieve variable definitions and human-readable descriptions for a table code.

    Args:
        table_code: ACS table code (e.g. 'B01001' for Sex by Age).
        limit: Maximum number of variables to return in the list preview (default: 50).
    """
    load_variable_definitions()
    vars_dict = get_variable_descriptions(table_code)
    sorted_vars = sorted(vars_dict.items())

    return {
        "table_code": table_code,
        "total_variables": len(vars_dict),
        "variables": [
            {"code": code, "label": label} for code, label in sorted_vars[:limit]
        ],
        "has_more": len(vars_dict) > limit,
    }


# ---------------------------------------------------------------------------
# Data tools
# ---------------------------------------------------------------------------


@mcp.tool()
def fetch_table_data(
    year: int,
    state_code: Optional[str],
    table_code: str,
    geo_level: str = "place",
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
    tract_code: Optional[str] = None,
    zcta_code: Optional[str] = None,
    msa_code: Optional[str] = None,
    dataset: Optional[str] = None,
    compute_reliability: bool = False,
    clean_sentinels: bool = True,
    use_cache: bool = True,
    max_rows: int = 500,
) -> Dict[str, Any]:
    """Fetch live ACS 5-year data with human-readable variable column headers.

    Args:
        year: ACS 5-year end-year (e.g. 2019, 2021, 2022).
        state_code: 2-digit state FIPS code (e.g. '36'); not needed for 'us'/'msa'.
        table_code: Detailed ('B01001'), data profile ('DP03'), subject ('S1701')
            or comparison profile ('CP03') table code.
        geo_level: 'us', 'state', 'county', 'tract', 'place', 'zcta', or 'msa'.
        place_code: Required if geo_level is 'place' (e.g. '51000' for New York city).
        county_code: County FIPS for 'county'/'tract' levels (default '*' = all).
        tract_code: Optional 6-digit tract code (default all tracts).
        zcta_code: ZCTA(s) for 'zcta' level, comma-separated (default '*').
        msa_code: CBSA code(s) for 'msa' level (default '*').
        dataset: 'acs5', 'profile', 'subject' or 'cprofile' (inferred if omitted).
        compute_reliability: Add standard error, CV % and reliability columns.
        clean_sentinels: Replace Census jam values (-666666666 etc.) with null.
        use_cache: Read/write the local SQLite query cache.
        max_rows: Maximum rows returned in 'data' (default 500).
    """
    client = _client(use_cache)
    try:
        raw_data = client.fetch(
            year=year,
            state_code=state_code,
            table_code=table_code,
            geo_level=geo_level,
            place_code=place_code,
            county_code=county_code,
            tract_code=tract_code,
            zcta_code=zcta_code,
            msa_code=msa_code,
            dataset=dataset,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc), "data": []}

    if not raw_data or len(raw_data) < 2:
        return {
            "success": False,
            "error": f"No data returned for year {year}, table {table_code}, "
            f"{geo_level} level. {client.last_error or ''}".strip(),
            "data": [],
        }

    dataset = dataset or infer_dataset(table_code)
    df = pd.DataFrame(raw_data[1:], columns=raw_data[0])
    df = df.loc[:, ~df.columns.duplicated()]  # NAME appears twice in group() calls
    df = add_geoid_column(df, geo_level)
    if clean_sentinels:
        df = clean_census_sentinels(df)
    if compute_reliability:
        df = add_reliability_columns(df)

    metadata = resolve_metadata(
        table_code, dataset, year, client if dataset != "acs5" else None
    )
    rename: Dict[str, str] = {}
    if dataset == "acs5":
        load_variable_definitions()
        rename = get_variable_descriptions(table_code)
    for code, label in describe_columns(df.columns, metadata).items():
        if code not in IDENTIFIER_COLUMNS and code not in rename:
            rename[code] = label
    formatted_df = df.rename(columns=rename)
    formatted_df = formatted_df.astype(object).where(pd.notna(formatted_df), None)

    return {
        "success": True,
        "year": year,
        "state_code": state_code,
        "table_code": table_code,
        "dataset": dataset,
        "geo_level": geo_level,
        "row_count": len(formatted_df),
        "columns": list(formatted_df.columns),
        "data": formatted_df.head(max_rows).to_dict(orient="records"),
        "truncated": len(formatted_df) > max_rows,
    }


@mcp.tool()
def query_data_profile(
    profile_code: str,
    year: int = DEFAULT_YEAR,
    state_code: Optional[str] = None,
    geo_level: str = "state",
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
    zcta_code: Optional[str] = None,
    msa_code: Optional[str] = None,
    compute_reliability: bool = False,
    max_rows: int = 500,
) -> Dict[str, Any]:
    """Query an ACS 5-year Data Profile with pre-computed rates and percentages.

    DP02 social, DP03 economic, DP04 housing, DP05 demographic. Percent values
    are the *PE columns, their MOEs the *PM columns.

    Args:
        profile_code: 'DP02', 'DP03', 'DP04' or 'DP05'.
        year: ACS 5-year end-year (default 2022).
        state_code: 2-digit state FIPS (required except for 'us' / 'msa' / 'zcta').
        geo_level: 'us', 'state', 'county', 'tract', 'place', 'zcta', or 'msa'.
        place_code / county_code / zcta_code / msa_code: Entity filters.
        compute_reliability: Add SE / CV / reliability columns.
        max_rows: Maximum rows returned.
    """
    code = profile_code.strip().upper()
    if code not in DATASET_TABLES["profile"]:
        return {
            "success": False,
            "error": f"Unknown data profile '{profile_code}'. "
            f"Choose from {sorted(DATASET_TABLES['profile'])}.",
        }
    result = fetch_table_data(
        year=year,
        state_code=state_code,
        table_code=code,
        geo_level=geo_level,
        place_code=place_code,
        county_code=county_code,
        zcta_code=zcta_code,
        msa_code=msa_code,
        dataset="profile",
        compute_reliability=compute_reliability,
        max_rows=max_rows,
    )
    result["profile_title"] = DATASET_TABLES["profile"][code]
    return result


@mcp.tool()
def batch_download(
    state_code: Optional[str],
    table_code: str,
    start_year: int,
    end_year: int,
    geo_level: str = "place",
    place_codes: Optional[List[str]] = None,
    output_format: str = "both",
    output_dir: Optional[str] = None,
    county_codes: Optional[List[str]] = None,
    zcta_codes: Optional[List[str]] = None,
    msa_codes: Optional[List[str]] = None,
    dataset: Optional[str] = None,
    clean_sentinels: bool = True,
    compute_reliability: bool = False,
    inflation_base_year: Optional[int] = None,
    include_codebook: bool = False,
    use_cache: bool = True,
    slug_style: str = "label",
) -> Dict[str, Any]:
    """Batch-download a table across years and geographies to research-ready files.

    Args:
        state_code: 2-digit state FIPS (e.g. '36'); optional for 'us', 'msa', 'zcta'.
        table_code: Table code ('B01001', 'DP03', 'S1701', 'CP03').
        start_year: Starting ACS 5-year end-year (e.g. 2015).
        end_year: Ending year (e.g. 2022).
        geo_level: 'us', 'state', 'county', 'tract', 'place', 'zcta', or 'msa'.
        place_codes: Place FIPS codes (required for 'place').
        output_format: 'both', 'wide_only', 'melted_only', 'excel',
            'panel' (tidy GEOID x Year CSV) or 'parquet' (tidy panel, snappy Parquet).
        output_dir: Destination folder (default 'downloads' in workspace root).
        county_codes: Counties for 'county' or 'tract' (tract: one request per
            county; omitted = all counties / all tracts in the state).
        zcta_codes: ZCTAs for 'zcta' (omitted = all ZIPs in state via zip2fips.json).
        msa_codes: CBSA codes for 'msa' (omitted = all metro/micro areas).
        dataset: 'acs5', 'profile', 'subject', 'cprofile' (inferred if omitted).
        clean_sentinels: Replace Census jam values (-666666666 etc.) with null.
        compute_reliability: Add SE, CV % and reliability columns per estimate.
        inflation_base_year: Convert dollar variables to constant dollars of
            this year using CPI-U.
        include_codebook: Write <table>_codebook.csv alongside the data.
        use_cache: Use the local SQLite query cache (.cache/census_cache.db).
        slug_style: Panel column names: 'label' (readable) or 'code' (Stata-safe).
    """
    if start_year > end_year:
        return {
            "success": False,
            "error": "start_year must be less than or equal to end_year.",
        }

    base_dir = output_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "downloads"
    )

    codes: Optional[List[str]] = None
    names: Dict[str, str] = {}
    if geo_level == "place":
        codes = place_codes
        places_file = get_local_places_file(state_code or "")
        if places_file and os.path.exists(places_file):
            names = {p[1]: p[2] for p in load_places_from_local(places_file)}
    elif geo_level in ("county", "tract"):
        codes = county_codes
    elif geo_level == "zcta":
        codes = zcta_codes
    elif geo_level == "msa":
        codes = msa_codes

    try:
        entities = build_entities(geo_level, state_code, codes, names)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    options = ResearchOptions(
        clean_sentinels=clean_sentinels,
        compute_reliability=compute_reliability,
        inflation_base_year=inflation_base_year,
        include_codebook=include_codebook,
        use_cache=use_cache,
        slug_style=slug_style,
    )
    return run_batch(
        table_code=table_code,
        entities=entities,
        years=list(range(start_year, end_year + 1)),
        output_dir=base_dir,
        output_format=output_format,
        options=options,
        dataset=dataset,
    )


# ---------------------------------------------------------------------------
# Research statistics tools
# ---------------------------------------------------------------------------


@mcp.tool()
def calculate_cv(estimate: float, moe: float) -> Dict[str, Any]:
    """Standard error, coefficient of variation and reliability for an ACS estimate.

    SE = MOE / 1.645; CV = SE / estimate x 100.
    Reliability: High (CV <= 15%), Moderate (15-30%), Low (> 30%).

    Args:
        estimate: The ACS estimate.
        moe: Its published 90% margin of error.
    """
    return _calculate_cv(estimate, moe)


@mcp.tool()
def calculate_significance(
    est1: float,
    moe1: float,
    est2: float,
    moe2: float,
    confidence_level: float = 0.90,
    year1: Optional[int] = None,
    year2: Optional[int] = None,
) -> Dict[str, Any]:
    """Two-tailed Z-test for whether two ACS estimates differ significantly.

    Z = |X1 - X2| / sqrt(SE1^2 + SE2^2). When year1 and year2 are both given
    (same geography, different 5-year periods), the Census overlapping-sample
    adjustment sqrt(1 - overlap/5) is applied to the SE of the difference.

    Args:
        est1, moe1: First estimate and its 90% MOE.
        est2, moe2: Second estimate and its 90% MOE.
        confidence_level: 0.90, 0.95 or 0.99 (default 0.90, the ACS standard).
        year1, year2: Optional ACS 5-year end years for the overlap adjustment.
    """
    overlap = acs5_overlap_years(year1, year2) if year1 and year2 else 0
    return z_test(est1, moe1, est2, moe2, confidence_level, overlap_years=overlap)


@mcp.tool()
def adjust_inflation(value: float, from_year: int, to_year: int) -> Dict[str, Any]:
    """Convert a nominal dollar value between years using the BLS CPI-U annual average.

    Args:
        value: Dollar amount in from_year dollars (e.g. 2012 ACS median income).
        from_year: Year of the original dollars (the ACS period end year).
        to_year: Target constant-dollar year.
    """
    try:
        adjusted = _adjust_inflation(value, from_year, to_year)
    except KeyError as exc:
        return {"success": False, "error": str(exc.args[0])}
    return {
        "success": True,
        "value": value,
        "from_year": from_year,
        "to_year": to_year,
        "cpi_from": CPI_U_ANNUAL[from_year],
        "cpi_to": CPI_U_ANNUAL[to_year],
        "adjusted_value": adjusted,
        "source": "BLS CPI-U annual average (CUUR0000SA0)",
    }


@mcp.tool()
def generate_codebook(
    table_code: str,
    year: int = DEFAULT_YEAR,
    dataset: Optional[str] = None,
    include_moe: bool = True,
    include_reliability: bool = False,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Emit a machine-readable data dictionary for a table.

    Columns: Variable_Code, Variable_Slug, Measurement_Type, Full_Description,
    Concept, Universe, Unit.

    Args:
        table_code: e.g. 'B19013', 'DP03', 'S1701'.
        year: Metadata vintage for non-detailed tables (default 2022).
        dataset: Override endpoint ('acs5', 'profile', 'subject', 'cprofile').
        include_moe: Include margin-of-error variables.
        include_reliability: Include derived SE / CV / reliability columns.
        output_path: Optional CSV path to also write the codebook to.
    """
    dataset = dataset or infer_dataset(table_code)
    client = _client() if dataset != "acs5" else None
    metadata = resolve_metadata(table_code, dataset, year, client)
    if not metadata:
        return {"success": False, "error": f"No variable metadata for {table_code}."}

    columns: List[str] = []
    for code in sorted(metadata):
        if not code.startswith(f"{table_code}_"):
            continue
        if code.endswith("PE"):
            base, moe, pct = code[:-2], f"{code[:-2]}PM", True
        elif code.endswith("E"):
            base, moe, pct = code[:-1], f"{code[:-1]}M", False
        else:
            continue
        columns.append(code)
        if include_moe:
            columns.append(moe)
        if include_reliability:
            key = f"{base}P" if pct else base
            columns += [f"{key}_SE", f"{key}_CV", f"{key}_RELIABILITY"]

    universe = load_table_universes().get(table_code, "") if dataset == "acs5" else ""
    codebook = _generate_codebook(columns, metadata, universe=universe)
    if output_path:
        codebook.to_csv(output_path, index=False, encoding="utf-8")
    return {
        "success": True,
        "table_code": table_code,
        "dataset": dataset,
        "variable_count": len(codebook),
        "output_path": output_path,
        "codebook": codebook.to_dict(orient="records"),
    }


def main() -> None:
    """Run the FastMCP server over standard I/O."""
    mcp.run()


if __name__ == "__main__":
    main()
