# PLAN_ROUND_4.md — US Census ACS Data Batch Downloader

**Planning Round:** 4  
**Date:** 2026-11-01  
**Branch:** phase4/mcp-server (to branch off main / defaultBranch once phase3 is merged)  
**Status:** Phase 3 COMPLETE. Ready to begin Phase 4.  
**Next Round:** Final round / Production readiness.

---

## Phase 3 Retrospective & Baseline State

Phase 3 successfully extracted the headless library layer:
- Clean package structure: `census/` (`api.py`, `geo.py`, `tables.py`, `export.py`).
- Automatic retry with exponential backoff for transient Census API errors (429, 500, 502, 503, 504).
- `acs_downloader.py` converted into a thin GUI consumer.
- Modularized unit test suite: 33 tests across 5 test files (`test_api`, `test_geo`, `test_tables`, `test_export`, `test_downloader`), all passing in ~0.15s.

---

## Phase 4 Plan — Model Context Protocol (MCP) Server

### Objectives
Build an MCP server (`mcp_server.py`) powered by the headless `census/` library. This allows Claude Desktop, Cursor, and other agentic coding environments to inspect Census geographies, explore tables/variables, and trigger data extraction pipelines directly via standard MCP tool calls.

### Proposed Architecture

```text
mcp_server.py
├── Server Initialization (mcp.server.fastmcp or FastMCP)
├── Tools:
│   ├── list_states() -> List[Dict]
│   ├── list_places(state_code: str, search: Optional[str]) -> List[Dict]
│   ├── search_tables(query: str, limit: int = 20) -> List[Dict]
│   ├── list_variables(table_code: str, limit: int = 50) -> List[Dict]
│   ├── fetch_table_data(year: int, state_code: str, table_code: str, geo_level: str, ...) -> Dict
│   └── batch_download(state_code: str, table_code: str, years: List[int], ...) -> Dict
└── Client Configuration Guide:
    ├── claude_desktop_config.json
    └── cursor_config.json
```

### Detailed Work Breakdown

#### 4a — Environment & Dependencies
1. Check or install `mcp` (`pip install mcp`).
2. Add `mcp>=1.0.0` to `requirements.txt`.

#### 4b — MCP Server Implementation (`mcp_server.py`)
1. Implement `mcp_server.py` using `FastMCP`:
   - `@mcp.tool() def list_states() -> List[Dict[str, str]]`:
     Returns all supported states/territories with FIPS codes.
   - `@mcp.tool() def list_places(state_code: str, search: str = "") -> List[Dict[str, str]]`:
     Returns places in the state, optionally filtered by keyword.
   - `@mcp.tool() def search_tables(query: str, limit: int = 25) -> List[Dict[str, str]]`:
     Searches `groups.json` by table code or title description.
   - `@mcp.tool() def list_variables(table_code: str, limit: int = 50) -> List[Dict[str, str]]`:
     Returns variable codes and descriptions for the specified table.
   - `@mcp.tool() def fetch_table_data(year: int, state_code: str, table_code: str, geo_level: str = "place", place_code: Optional[str] = None, county_code: Optional[str] = None) -> Dict[str, Any]`:
     Queries live Census ACS5 API and returns formatted JSON rows.
   - `@mcp.tool() def batch_download(state_code: str, table_code: str, start_year: int, end_year: int, geo_level: str = "place", place_codes: Optional[List[str]] = None, output_format: str = "both", output_dir: Optional[str] = None) -> Dict[str, Any]`:
     Executes batch downloads across years/entities and writes files to disk.

#### 4c — Automated Tests & Verification
1. Add `tests/test_mcp.py` to test MCP tool registrations and execution.
2. Verify with standard test runner: `python -m unittest discover tests`.

#### 4d — Documentation
1. Provide copy-paste MCP configuration snippets for:
   - Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json` on Windows).
   - Cursor (`.cursor/mcp.json`).

---

## Notes for Next Agent

- Active branch to merge: `phase3/headless-library` -> `defaultBranch`.
- New branch for Phase 4: `git checkout -b phase4/mcp-server`.
- All 33 unit tests pass.
- Adhere to Senior Principal Engineer persona: deliberate planning, explicit typing, black formatting, zero placeholders.

