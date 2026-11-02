# PLAN_ROUND_3.md — US Census ACS Data Batch Downloader

**Planning Round:** 3  
**Date:** 2026-11-01  
**Branch:** phase3/headless-library (to branch off main / defaultBranch once phase2 is merged)  
**Status:** Phase 2 COMPLETE. Ready to begin Phase 3.  
**Next Round:** Create PLAN_ROUND_4.md when Phase 3 is complete.

---

## Phase 2 Retrospective & Baseline State

Phase 2 completed the Research UX overhaul:
- Threading with freeze prevention and thread-safe queue updates.
- Dracula-themed scrollable progress & status log.
- Multi-place selection with real-time text filter.
- Dynamic call estimation with safety warning threshold (>50 calls).
- Flexible export formats: Wide CSV, Melted CSV, Both, and multi-sheet Excel (.xlsx).
- Variable browser with lazy loading and structured previews.
- Geography level selector: Place, State, County.
- Insertion of explicit `Year` column at column 0 in wide & melted datasets.
- 13 automated unit tests in `tests/test_downloader.py` (all passing, black formatted).

---

## Phase 3 Plan — Headless Library Layer Extraction

### Objectives
Extract all Census API, geography, table parsing, and data export logic from `acs_downloader.py` into a modular, reusable Python package (`census/`). `acs_downloader.py` will then become a thin Tkinter view layer that imports cleanly from `census`.

### Proposed Package Structure
```text
census/
├── __init__.py          # Exports public API (fetch, load, export functions)
├── api.py              # Census API client, retry & exponential backoff
├── geo.py              # State mappings, place loading & geographic resolution
├── tables.py           # Table groups & variable definitions loading/caching
└── export.py           # DataFrame formatting, deduplication, melting, CSV & Excel export
```

### Detailed Work Breakdown

#### 3a — Modular Package Extraction
1. `census/geo.py`:
   - `STATE_FILES`, `STATE_MAPPING`, `FIPS_MAPPING`
   - `extract_state_abbr_from_filename(filename: str) -> str`
   - `get_local_places_file(state_abbr: str) -> Optional[str]`
   - `load_places_from_local(path: str) -> List[Tuple[str, str, str, str, str]]`
2. `census/tables.py`:
   - `load_tables_from_local(path: str) -> Dict[str, str]`
   - `load_variable_definitions(filepath: Optional[str] = None) -> Dict[str, str]`
   - `get_variable_descriptions(table_code: str, definitions: Optional[Dict[str, str]] = None) -> Dict[str, str]`
3. `census/export.py`:
   - `format_dataframe(df: pd.DataFrame, variable_descriptions: Dict[str, str]) -> pd.DataFrame`
   - `handle_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame`
   - `enhanced_melt(df: pd.DataFrame, id_vars: List[str], var_name: str, value_name: str) -> pd.DataFrame`
   - `sanitize_filename(name: str) -> str`
   - `process_data_and_save(...) -> None`
   - `save_excel_workbook(...) -> Optional[str]`
4. `census/api.py`:
   - `fetch_census_data(...) -> Optional[List[List[str]]]`
   - Add robust HTTP retry logic with exponential backoff using `requests.adapters.HTTPAdapter` and `urllib3.util.Retry` for status codes 429, 500, 502, 503, 504.

#### 3b — Refactor `acs_downloader.py`
- Replace inline data logic with imports from `census`.
- Retain Tkinter UI setup, style definitions, and background threading queue polling.
- Ensure backwards compatibility with any existing scripts.

#### 3c — Test Suite Modularization
- Split or expand `tests/` into:
  - `tests/test_api.py`
  - `tests/test_geo.py`
  - `tests/test_tables.py`
  - `tests/test_export.py`
  - `tests/test_gui.py`

---

## Phase 4 (Preview) — MCP Server

Once the `census` headless package is stable, build `mcp_server.py`:
- Install `mcp` SDK.
- Expose tools: `list_states`, `list_places`, `search_tables`, `list_variables`, `fetch_census_data`, `batch_download`.
- Generate client configuration guides for Claude Desktop and Cursor.

---

## Notes for Next Agent

- Current active branch: `phase2/research-ux`
- Merge `phase2/research-ux` into `defaultBranch` before starting Phase 3.
- New branch for Phase 3: `git checkout -b phase3/headless-library`
- All unit tests pass with `python -m unittest discover tests`.
- Adhere to Senior Principal Engineer persona: explicit typing, black formatting, deliberate planning.

