# PLAN_ROUND_3.md — US Census ACS Data Batch Downloader

**Planning Round:** 3  
**Date:** 2026-11-01  
**Branch:** phase3/headless-library  
**Status:** Phase 3 COMPLETE. Ready to begin Phase 4.  
**Next Round:** See PLAN_ROUND_4.md for Phase 4 (Model Context Protocol / MCP Server).

---

## Phase 3 Completion Summary

All Phase 3 tasks have been executed, verified, and committed on branch `phase3/headless-library`.

### Implemented Architecture

1. **3a — Headless Library Package (`census/`)**
   - **`census/geo.py`**:
     - Embedded reference data: `STATE_FILES`, `STATE_MAPPING`, `FIPS_MAPPING`.
     - `extract_state_abbr_from_filename(filename: str) -> str`: FIPS code parser.
     - `get_local_places_file(state_abbr: str, base_dir: Optional[str] = None) -> Optional[str]`: Resolves state file paths in dev or PyInstaller bundles.
     - `load_places_from_local(path: str) -> List[Tuple[str, str, str, str, str]]`: Reads pipe-delimited Census places.
   - **`census/tables.py`**:
     - `load_tables_from_local(path: str) -> Dict[str, str]`: Reads table codes and descriptions from `groups.json`.
     - `load_variable_definitions(filepath: Optional[str] = None, force_reload: bool = False) -> Dict[str, str]`: Cached reader for `variables.json`.
     - `get_variable_descriptions(table_code: str, definitions: Optional[Dict[str, str]] = None) -> Dict[str, str]`: Filters variable descriptions by table prefix.
   - **`census/api.py`**:
     - `CensusAPIClient`: Configurable class managing connection pooling and exponential backoff retry via `requests.adapters.HTTPAdapter` and `urllib3.util.Retry` for status codes 429, 500, 502, 503, and 504.
     - Method `fetch(year, state_code, table_code, geo_level, place_code, county_code)` supporting Place, State, and County queries.
     - Convenience function `fetch_census_data(...)` maintaining signature backwards compatibility.
   - **`census/export.py`**:
     - `format_dataframe(df, table_code, variable_descriptions)`: Renames raw variable codes to human-readable labels.
     - `handle_duplicate_columns(df)`: Deduplicates column names with sequential suffixes.
     - `enhanced_melt(df, id_vars, var_name, value_name)`: Reshapes from wide to long format with missing column checks.
     - `sanitize_filename(name)`: Punctuation and whitespace cleaner.
     - `process_data_and_save(...)`: Writes wide/melted CSVs with explicit `Year` column at index 0.
     - `save_excel_workbook(...)`: Compiles wide and melted data into `.xlsx` with `Wide_Data` and `Melted_Data` sheets.
   - **`census/__init__.py`**:
     - Exposes all core classes and functions at package level.

2. **3b — Refactored `acs_downloader.py`**
   - Converted into a thin Tkinter view layer importing from `census`.
   - Maintains exact Dracula UI styling, threading queue, and backward compatibility.

3. **3c — Test Suite Modularization**
   - Expanded unit tests into targeted modules:
     - `tests/test_geo.py`: Geographic parsing and mapping tests.
     - `tests/test_tables.py`: Table and variable definition caching tests.
     - `tests/test_api.py`: Client initialization, parameter construction, and mock queries.
     - `tests/test_export.py`: Deduplication, melting, CSV, and Excel export tests.
     - `tests/test_downloader.py`: End-to-end integration and GUI initialization tests.
   - Total tests: 33 tests, all passing, 100% formatted to `black` conventions.

---

## Known Remaining Issues / Transition to Phase 4

1. **MCP Server**: With the headless `census/` package complete, the next phase is building `mcp_server.py` to expose Census tools to Claude Desktop, Cursor, and agentic workflows.
2. **MCP SDK**: `mcp` library will need to be installed in Phase 4.
