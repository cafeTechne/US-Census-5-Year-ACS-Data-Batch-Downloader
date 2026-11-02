# PLAN_ROUND_2.md — US Census ACS Data Batch Downloader

**Planning Round:** 2  
**Date:** 2026-11-01  
**Branch:** phase1/cleanup (complete, not yet merged to main)  
**Status:** Phase 1 COMPLETE. Ready to begin Phase 2.  
**Next Round:** Create PLAN_ROUND_3.md when Phase 2 is complete.

---

## Phase 1 Completion Summary

All Phase 1 tasks are done and committed on branch `phase1/cleanup`.

### What was fixed

**Bug 1 - format_dataframe TypeError (FIXED)**
`VARIABLE_DEFINITIONS` stores flat strings. The old code tried to index them as dicts.
Fixed by simplifying to a direct `df.rename(columns=variable_descriptions)`.

**Bug 2 - process_data_and_save never wrote data (FIXED)**
The guard `if "GEO_ID" in formatted_df.columns` was always False.
Fixed to `if "state" in formatted_df.columns` (actual Census API column name).
`enhanced_melt` id_vars updated to `["NAME", "state", "place"]` (dynamic — only includes cols that exist).

**Bug 3 - API key hardcoded (FIXED)**
API key moved to `.env` file via `python-dotenv`.
`.env` is in `.gitignore`. `.env.example` provided as template.
`API_KEY = os.getenv("CENSUS_API_KEY", "")` in acs_downloader.py.

**Bug 4 - Year range hardcoded (FIXED)**
Added From/To Spinbox widgets to the UI (row 2, defaults: 2010-2022, range: 2005-2023).
`on_submit()` reads and validates the spinbox values before the fetch loop.

### Additional improvements
- Deleted dead files: dickbird.py, app.py, tempCodeRunnerFile.py
- Removed duplicate global variables (json_file, frozen base_path block)
- Removed debug print spam from enhanced_melt
- Added null-safe place lookup in on_submit (no more bare next() crash)
- Added empty-place guard in on_submit
- Added completion messagebox.showinfo() after download loop
- Added requirements.txt, .gitignore, .env.example
- All fixed functions now have type annotations and docstrings

### Files changed
- acs_downloader.py  (primary — all fixes here)
- .env               (created — gitignored, contains real key)
- .env.example       (created — safe to commit, placeholder key)
- .gitignore         (created)
- requirements.txt   (created: requests, pandas, openpyxl, python-dotenv)
- PLAN_ROUND_1.md    (created)
- dickbird.py        (DELETED)
- app.py             (DELETED)
- tempCodeRunnerFile.py (DELETED)

---

## Known Remaining Issues / Things to Verify

1. The app currently FREEZES the UI during the fetch loop.
   range(2010, 2022) = 13 API calls, each can take 1-3 seconds.
   This must be moved to a background thread in Phase 2.

2. The description_text widget (Tkinter Text) uses SELECTION_COLOR background,
   which may make it hard to read depending on platform rendering.
   Minor visual issue, address in Phase 2 styling pass.

3. The table listbox shows only table codes (e.g. "B01001") not descriptions.
   The description only appears when you click. A combined display would help.
   Address in Phase 2.

4. No end-to-end test has been run yet against the live Census API.
   MUST verify before marking Phase 1 truly done:
   - Pick any state (e.g. New York, code 36)
   - Pick a place (e.g. "New York city")
   - Pick a table (e.g. B01001 - Sex by Age)
   - Set year range 2019-2019 (single year to be fast)
   - Click Download Data
   - Confirm a CSV is written with actual data rows

---

## Phase 2 Plan — Research Tool UX Improvements

Merge branch first: `git checkout main && git merge phase1/cleanup`
Create new branch: `git checkout -b phase2/research-ux`

### 2a — Threading (Critical, do first)
The UI freezes during downloads because the fetch loop runs on the main thread.

Plan:
- Move the download loop from on_submit() into a worker function
- Run it in threading.Thread(target=worker, daemon=True)
- Disable the Submit button while running (prevent double-submit)
- Show a status label that updates per year ("Fetching 2015...")
- Re-enable button and show completion message when done
- Use app.after() or a queue to push status updates back to the main thread safely

### 2b — Progress / Status Log
Add a scrollable status log below the submit button:
- tk.Text in readonly mode (state=DISABLED, toggle to NORMAL to insert)
- One line per year: "2015: 42 rows saved" or "2015: skipped (no data)"
- Clear log on each new submission

### 2c — Multi-place Selection
Allow user to select multiple places for the same table/year range:
- Change combo (place Combobox) to a Listbox with selectmode=MULTIPLE
- or add an "Add place" button that accumulates a list
- Outer loop: for each place, for each year

### 2d — Year Range UX Hardening
- Display how many API calls will be made before submitting
  e.g. "3 places x 13 years = 39 API calls"
- Warn if that number is large (>50)

### 2e — Output Format Options
Add radio buttons or a dropdown:
  Wide CSV only (compiled_unmelted_data.csv)
  Melted CSV only (per-year files)
  Both (current default)
  Excel (.xlsx via openpyxl, one sheet per year or combined)

### 2f — Variable Browser Enhancement
Currently the description_text shows only the table description (from groups.json).
After a table is selected, optionally show the first N variable labels from VARIABLE_DEFINITIONS.
Load lazily (only when table is clicked, not at startup).

### 2g — Geography Level Selector (Stretch goal for Phase 2)
The Census API supports multiple geography levels beyond "place":
  for=county:*&in=state:{state_code}
  for=tract:*&in=state:{state_code}&in=county:{county_code}
  for=state:{state_code}

Add a radio button group: Place / County / State / National
Adjust the place picker and API params accordingly.

---

## Phase 3 (Reminder) — Headless Library Layer

Before Phase 4 (MCP), extract all non-GUI logic into census/ package:
  census/__init__.py
  census/api.py     - fetch_census_data, with retry/backoff
  census/geo.py     - load_places_from_local, get_local_places_file, STATE_MAPPING
  census/tables.py  - load_tables_from_local, load_variable_definitions, get_variable_descriptions
  census/export.py  - process_data_and_save, enhanced_melt, handle_duplicate_columns

acs_downloader.py becomes thin: imports from census/, only contains Tkinter code.
Add type annotations throughout and basic pytest tests.

---

## Phase 4 (Reminder) — MCP Server

After Phase 3:
  pip install mcp
  Create mcp_server.py
  Tools: list_states, list_places, search_tables, list_variables,
         fetch_year, batch_fetch, export_csv

Document config for Claude Desktop (claude_desktop_config.json) and Cursor.

---

## Notes for Next Agent

- Canonical file: acs_downloader.py
- Branch to merge first: phase1/cleanup -> main
- New branch for Phase 2: git checkout -b phase2/research-ux
- Python version in use: 3.12
- python-dotenv 1.2.1 is already installed in the user environment
- .env file exists with real Census API key (gitignored) - DO NOT delete it
- The Dracula color palette is intentional - preserve BACKGROUND_COLOR, etc.
- ACS5 years available: roughly 2005-2022 (varies by table)
  The Census API returns HTTP 400 for years/tables that don't exist - handle gracefully
- Census API rate limit: not officially published but ~500 req/day per key is safe
  The app does not currently implement retry or backoff - consider adding in Phase 3
