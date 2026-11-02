"""US Census ACS Batch Downloader — MCP Server for Claude Code & Claude Desktop.

Exposes Census data query, table exploration, and batch download tools over the
Model Context Protocol (MCP).
"""

import os
import sys
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
import pandas as pd

from census import (
    STATE_MAPPING,
    fetch_census_data,
    format_dataframe,
    get_local_places_file,
    get_resource_path,
    get_variable_descriptions,
    load_places_from_local,
    load_tables_from_local,
    load_variable_definitions,
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)

load_dotenv()

# Initialize FastMCP Server
mcp = FastMCP("US-Census-ACS-Downloader")


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
                }
            )
            if len(results) >= limit:
                break

    return results


@mcp.tool()
def search_tables(query: str, limit: int = 25) -> List[Dict[str, str]]:
    """Search Census ACS 5-year table groups by code or keyword.

    Args:
        query: Table code (e.g. 'B01001') or keyword (e.g. 'poverty', 'income').
        limit: Maximum number of matching tables to return (default: 25).
    """
    groups_file = get_resource_path("groups.json")
    if not os.path.exists(groups_file):
        return []

    all_tables = load_tables_from_local(groups_file)
    q = query.strip().lower()
    matches: List[Dict[str, str]] = []

    for code, desc in sorted(all_tables.items()):
        if q in code.lower() or q in desc.lower():
            matches.append({"table_code": code, "description": desc})
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


@mcp.tool()
def fetch_table_data(
    year: int,
    state_code: str,
    table_code: str,
    geo_level: str = "place",
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch live ACS 5-year data with human-readable variable column headers.

    Args:
        year: ACS 5-year end-year (e.g. 2019, 2021, 2022).
        state_code: 2-digit state FIPS code (e.g. '36').
        table_code: ACS table code (e.g. 'B01001').
        geo_level: Geography level ('place', 'state', or 'county').
        place_code: Required if geo_level is 'place' (e.g. '51000' for New York city).
        county_code: Optional county FIPS code if geo_level is 'county' (defaults to '*' for all counties).
    """
    raw_data = fetch_census_data(
        year=year,
        state_code=state_code,
        table_code=table_code,
        geo_level=geo_level,
        place_code=place_code,
        county_code=county_code,
    )

    if not raw_data or len(raw_data) < 2:
        return {
            "success": False,
            "error": f"No data returned for year {year}, table {table_code}, {geo_level} level.",
            "data": [],
        }

    df = pd.DataFrame(raw_data[1:], columns=raw_data[0])
    formatted_df = format_dataframe(df, table_code)

    return {
        "success": True,
        "year": year,
        "state_code": state_code,
        "table_code": table_code,
        "geo_level": geo_level,
        "row_count": len(formatted_df),
        "columns": list(formatted_df.columns),
        "data": formatted_df.to_dict(orient="records"),
    }


@mcp.tool()
def batch_download(
    state_code: str,
    table_code: str,
    start_year: int,
    end_year: int,
    geo_level: str = "place",
    place_codes: Optional[List[str]] = None,
    output_format: str = "both",
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a batch download across multiple years and entities, exporting CSV or Excel files.

    Args:
        state_code: 2-digit state FIPS code (e.g. '36').
        table_code: ACS table code (e.g. 'B01001').
        start_year: Starting year (e.g. 2018).
        end_year: Ending year (e.g. 2022).
        geo_level: Geography level ('place', 'state', or 'county').
        place_codes: List of place FIPS codes if geo_level is 'place' (e.g. ['51000', '01000']).
        output_format: One of 'both', 'wide_only', 'melted_only', 'excel'.
        output_dir: Destination folder path (defaults to 'downloads' in workspace root).
    """
    if start_year > end_year:
        return {
            "success": False,
            "error": "start_year must be less than or equal to end_year.",
        }

    base_dir = output_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "downloads"
    )
    os.makedirs(base_dir, exist_ok=True)

    entities: List[tuple[str, Optional[str]]] = []

    if geo_level == "place":
        if not place_codes:
            return {
                "success": False,
                "error": "place_codes must be provided when geo_level is 'place'.",
            }
        places_file = get_local_places_file(state_code)
        all_places = (
            load_places_from_local(places_file)
            if (places_file and os.path.exists(places_file))
            else []
        )
        code_to_name = {p[1]: p[2] for p in all_places}
        for code in place_codes:
            name = code_to_name.get(code, f"Place_{code}")
            entities.append((name, code))
    elif geo_level == "state":
        state_name = STATE_MAPPING.get(state_code, f"State_{state_code}")
        entities.append((state_name, None))
    elif geo_level == "county":
        state_name = STATE_MAPPING.get(state_code, f"State_{state_code}")
        entities.append((f"{state_name}_All_Counties", "*"))
    else:
        return {
            "success": False,
            "error": f"Unsupported geo_level: {geo_level}",
        }

    years = list(range(start_year, end_year + 1))
    total_calls = len(entities) * len(years)
    successful_calls = 0
    saved_entities: List[Dict[str, Any]] = []

    for entity_name, place_or_county in entities:
        clean_name = sanitize_filename(entity_name)
        folder = os.path.join(base_dir, f"{clean_name}_{table_code}")
        os.makedirs(folder, exist_ok=True)
        acc: Dict[str, List[pd.DataFrame]] = {"wide": [], "melted": []}
        years_saved: List[int] = []

        for yr in years:
            data = fetch_census_data(
                year=yr,
                state_code=state_code,
                table_code=table_code,
                geo_level=geo_level,
                place_code=place_or_county if geo_level == "place" else None,
                county_code=place_or_county if geo_level == "county" else None,
            )
            if data and len(data) > 1:
                df = pd.DataFrame(data[1:], columns=data[0])
                formatted_df = format_dataframe(df, table_code)
                process_data_and_save(
                    formatted_df=formatted_df,
                    folder_name=folder,
                    entity_name=entity_name,
                    table_code=table_code,
                    year=yr,
                    output_format=output_format,
                    excel_accumulator=acc,
                )
                years_saved.append(yr)
                successful_calls += 1

        excel_path = None
        if output_format == "excel":
            excel_path = save_excel_workbook(folder, entity_name, table_code, acc)

        saved_entities.append(
            {
                "entity": entity_name,
                "folder": folder,
                "years_saved": years_saved,
                "excel_path": excel_path,
            }
        )

    return {
        "success": True,
        "base_directory": base_dir,
        "total_calls_attempted": total_calls,
        "successful_calls": successful_calls,
        "format": output_format,
        "entities": saved_entities,
    }


def main() -> None:
    """Run the FastMCP server over standard I/O."""
    mcp.run()


if __name__ == "__main__":
    main()

