# PLAN_ROUND_2.md — US Census ACS Data Batch Downloader

**Planning Round:** 2  
**Date:** 2026-11-01  
**Branch:** phase2/research-ux  
**Status:** Phase 2 COMPLETE. Ready to begin Phase 3.  
**Next Round:** See PLAN_ROUND_3.md for Phase 3 (Headless Library Layer).

---

## Phase 2 Completion Summary

All Phase 2 tasks are complete and verified on branch `phase2/research-ux`.

### Implemented Enhancements

1. **2a — Threading & Freeze Prevention (COMPLETE)**
   - Moved the fetch loop from `on_submit()` into `_download_worker()`.
   - Executed in a background daemon thread (`threading.Thread(target=..., daemon=True)`).
   - Submit button is disabled and its label changed to "Downloading..." during operation to prevent duplicate execution.
   - Status label updates dynamically per entity and per year.
   - Main thread polls a thread-safe `queue.Queue` every 50ms via `root.after()`, guaranteeing 100% thread safety for Tkinter widget updates.

2. **2b — Progress / Status Log (COMPLETE)**
   - Added a scrollable Dracula-themed `tk.Text` log (`SELECTION_COLOR` background, `FOREGROUND_COLOR` text) with vertical scrollbar.
   - Read-only (`state=tk.DISABLED`), automatically scrolling to `tk.END` on new entries.
   - Clears automatically at the start of each submission.
   - Logs timestamped status updates per entity, per year, rows/columns processed, and skipped years (e.g. unavailable ACS years).

3. **2c — Multi-Place Selection (COMPLETE)**
   - Replaced single Combobox with a multi-select Listbox (`selectmode=tk.EXTENDED`).
   - Added real-time text filter entry to quickly search and isolate places.
   - Added helper buttons: "Select All" and "Clear".
   - Added real-time counter label showing selected count (e.g., "5 selected").
   - Worker loops across all selected places and years.

4. **2d — Year Range UX Hardening & Call Estimator (COMPLETE)**
   - Added real-time calculation and display of total estimated API calls: `entities x years`.
   - Color-coded indicator: GREEN (≤ 50), ORANGE (51–150), RED (> 150).
   - Added safety confirmation dialog before starting if total calls exceed 50 to prevent unintentional API throttling.

5. **2e — Output Format Options (COMPLETE)**
   - Added format dropdown supporting:
     - "Both Wide & Melted CSVs" (default)
     - "Melted CSV only"
     - "Wide CSV only"
     - "Excel Workbook (.xlsx)" (using `openpyxl`, creates `Wide_Data` and `Melted_Data` sheets).
   - In both wide and melted data, a `Year` column is now prepended at index 0, resolving multi-year data disambiguation in compiled files.

6. **2f — Variable Browser Enhancement (COMPLETE)**
   - Selecting a table now lazily loads `VARIABLE_DEFINITIONS` without UI lag.
   - Description pane displays Table Code, Title, Total Variables count, and formatted preview of variable codes and labels.

7. **2g — Geography Level Selector (COMPLETE)**
   - Added radio buttons for:
     - "Place (Cities/CDPs)": `for=place:{place_code}&in=state:{state_code}`
     - "Entire State": `for=state:{state_code}`
     - "All Counties in State": `for=county:*&in=state:{state_code}`
   - Dynamic UI hides or shows place picker depending on chosen geography level.

8. **Architecture, Quality & Tests (COMPLETE)**
   - Refactored `acs_downloader.py` into a clean `ACSDownloaderApp` class with `if __name__ == "__main__":` entry point, making all functions importable headlessly.
   - Added comprehensive unit test suite in `tests/test_downloader.py` (13 tests covering melting, duplicate column handling, table loading, place parsing, file sanitization, export formats, and API parameter generation).
   - Strict adherence to `black` formatting and explicit type annotations.

---

## Known Remaining Issues / Transition to Phase 3

1. **Monolithic Architecture**: Non-GUI functions (`fetch_census_data`, `load_places_from_local`, `format_dataframe`, `process_data_and_save`) remain in `acs_downloader.py`. They should be extracted into a standalone headless `census/` package in Phase 3.
2. **API Resilience**: The app currently skips failed requests with a log entry; Phase 3 will introduce exponential backoff / retry logic in `census/api.py`.
3. **MCP Server**: Once the headless library is extracted, Phase 4 will expose tools over the Model Context Protocol.
