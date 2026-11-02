# PLAN_ROUND_1.md — US Census ACS Data Batch Downloader

**Planning Round:** 1  
**Date:** 2026-11-01  
**Status:** Not yet started — awaiting implementation  
**Next Round:** Create `PLAN_ROUND_2.md` when Phase 1 is complete, updating status and leaving notes for the next agent.

---

## Project Overview

A Python desktop tool (Tkinter GUI) that batch-downloads **ACS 5-Year estimates** from the [US Census Bureau API](https://api.census.gov/data.html) for a selected U.S. state, place, and table group across multiple years (originally 2010–2019). Outputs are saved as CSVs in both wide and long/melted formats.

**Owner:** cafeTechne  
**Repo (original):** https://github.com/cafeTechne/acs5year_place_downloader  
**API Docs:** https://www.census.gov/data/developers/data-sets/acs-5year.html  
**Census API Key:** Hardcoded in source files — see Phase 1 task to fix this.

---

## Current File Inventory

| File | Purpose | Keep? |
|---|---|---|
| `acs_downloader.py` | **Primary working Tkinter GUI** — the canonical app. Fix and extend this file. | Yes |
| `dickbird.py` | Byte-for-byte duplicate of `acs_downloader.py`. No meaningful changes. | Delete |
| `app.py` | Incomplete Flask web app skeleton. Not pursuing this direction. | Delete |
| `tempCodeRunnerFile.py` | VS Code Code Runner auto-generated scratch file. | Delete |
| `variables.json` | ~10MB local cache of all ACS variable definitions. | Keep |
| `groups.json` | Local cache of all ACS table group names and descriptions. | Keep |
| `state_places/` | State place files (pipe-delimited .txt) for FIPS resolution. | Keep |
| `fips_data.xlsx` | FIPS reference data. | Keep |
| `aggregated_data.xlsx` | Output artifact from a prior run. | Keep |
| `zip2fips.json` | ZIP-to-FIPS mapping (useful for future geography expansion). | Keep |
| `kitty.ico` | App icon for the Tkinter window. | Keep |
| `README.md` | Outdated. Update after work is complete. | Keep |

---

## Known Bugs (Must Fix in Phase 1)

### Bug 1 — format_dataframe TypeError (Critical)
**File:** `acs_downloader.py` — `format_dataframe()` (~line 145)

VARIABLE_DEFINITIONS stores flat strings (labels) but the code indexes them as dicts:
  attributes = VARIABLE_DEFINITIONS[var]["attributes"].split(",") ...

Fix: simplify to a direct rename since the value is already the label string:
  def format_dataframe(df, table_code):
      variable_descriptions = get_variable_descriptions(table_code)
      df.rename(columns=variable_descriptions, inplace=True)
      return df

### Bug 2 — process_data_and_save never writes data (Critical)
**File:** `acs_downloader.py` — `process_data_and_save()` (~line 191)

The guard checks for "GEO_ID" which the Census ACS5 API does NOT return by default.
The API actually returns: NAME, state, place, and the variable columns.
This condition is always False, so no data is ever saved.

Fix: change the guard to:
  if "NAME" in formatted_df.columns and "state" in formatted_df.columns:

And update enhanced_melt id_vars to ["NAME", "state", "place"].

### Bug 3 — API Key Hardcoded in Source
All Python files contain the raw API key committed to git.

Fix: Use python-dotenv.
  - Create .env with: CENSUS_API_KEY=<key>
  - Add .env to .gitignore
  - Provide .env.example
  - In acs_downloader.py: API_KEY = os.getenv("CENSUS_API_KEY", "")

### Bug 4 — Year Range Hardcoded
`on_submit()` has: for year in range(2010, 2020)
ACS 5-Year data is available through at least 2022.

Fix: Add start/end year spinboxes to the UI, defaulting to 2010 and the current available max.

---

## Architecture Decision

**Keep Tkinter. Do not migrate to Flask.**

The Flask migration in app.py / tempCodeRunnerFile.py was an incomplete detour.
The Tkinter app was working before and is the correct foundation.

Plan:
  1. Fix the Tkinter app (Phase 1)
  2. Improve it as a research tool (Phase 2)
  3. Extract a headless library layer (Phase 3)
  4. Build an MCP server on top of that layer (Phase 4)

---

## Roadmap

### Phase 1 — Cleanup and Bug Fixes
Goal: Get acs_downloader.py to a correct, runnable baseline.

- Delete dickbird.py, app.py, tempCodeRunnerFile.py
- Fix Bug 1: format_dataframe TypeError
- Fix Bug 2: process_data_and_save GEO_ID guard
- Fix Bug 3: Move API key to .env / python-dotenv
- Fix Bug 4: Make year range configurable (start/end year inputs in UI)
- Add requirements.txt (requests, pandas, openpyxl, python-dotenv)
- Verify end-to-end: pick a state/place/table, confirm CSVs are actually written
- Create PLAN_ROUND_2.md when done

### Phase 2 — Research Tool UX Improvements
Goal: Make the tool genuinely useful for research workflows.

- Multi-place selection (same table, multiple places at once)
- Multi-table selection
- Year range UI inputs (start/end spinboxes)
- Output format options: wide CSV, long/melted CSV, Excel, JSON
- Data preview pane (show sample before saving)
- Lazy variable browser (variables.json is 10MB — do not load at startup)
- Progress bar / status log for batch operations (use threading to prevent UI freeze)
- Geography level selector: place (current), county, census tract, state, national
- Friendly error handling for 404s, rate limits, network failures

### Phase 3 — Headless Library Layer
Goal: Extract all data logic into a clean importable Python module with no GUI dependency.
This is a prerequisite for the MCP server.

Create census/ package:
  census/__init__.py
  census/api.py      — Census API calls (fetch_census_data, fetch_variables, etc.)
  census/geo.py      — state/place file loading, FIPS lookups
  census/tables.py   — groups/table search
  census/export.py   — DataFrame to CSV/Excel/JSON

- acs_downloader.py imports from census/ (GUI becomes a thin wrapper)
- All functions fully type-annotated (Python type hints)
- Basic unit tests with pytest for pure data functions

### Phase 4 — MCP Server
Goal: Expose the tool to AI agents via the Model Context Protocol (https://modelcontextprotocol.io/).

Install: pip install mcp
Create: mcp_server.py

Tools to implement:
  list_states     — Return all U.S. states/territories with FIPS codes
  list_places     — Return places in a given state (by FIPS or name)
  search_tables   — Search ACS table groups by keyword
  list_variables  — Return variable descriptions for a given table code
  fetch_year      — Fetch ACS data for one year/state/place/table
  batch_fetch     — Fetch across a year range, return aggregated result
  export_csv      — Save current data to CSV, return file path

Document MCP server config for Claude Desktop and Cursor in README.

---

## Notes for Future Agents

- The canonical file to work on is acs_downloader.py.
  Do NOT work on dickbird.py, app.py, or tempCodeRunnerFile.py — delete them.
- Before non-trivial changes, create a git branch: git checkout -b phase1/cleanup
- variables.json is 10MB. It loads lazily (only on submit). Keep it that way.
- groups.json is loaded at startup to populate the table listbox — this is fine.
- State place files are in state_places/, pipe-delimited, with a header row to skip.
  Each line has 9 fields: state, state_code, place_code, _, place_name, place_type, _, _, counties
- Census API base URL: https://api.census.gov/data/{year}/acs/acs5
  Params: get=NAME,group({table_code}), for=place:{place_code}, in=state:{state_code}, key={API_KEY}
- ACS5 data availability: generally 2009-2022 (some tables start later).
  Check https://api.census.gov/data.html for year availability per table.
- The Dracula color scheme (BACKGROUND_COLOR="#282A36" etc.) is intentional — preserve it.
