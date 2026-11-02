# US Census ACS Batch Downloader — MCP Setup Guide

This guide details how to integrate the US Census ACS Data Batch Downloader with **Claude Code** and **Claude Desktop** via the Model Context Protocol (MCP).

---

## 1. Overview of MCP Tools

The `mcp_server.py` server exposes the following tools to Claude:

| Tool Name | Parameters | Description |
| :--- | :--- | :--- |
| `list_states` | None | Lists all 50 states and territories with 2-digit FIPS codes. |
| `list_places` | `state_code` (str), `search` (str), `limit` (int) | Lists incorporated cities, towns, and CDPs for a state. |
| `search_tables` | `query` (str), `limit` (int) | Searches ACS 5-year table groups by code (e.g. `B01001`) or keywords (e.g. `poverty`). |
| `list_variables` | `table_code` (str), `limit` (int) | Retrieves variables and human-readable definitions for a table. |
| `fetch_table_data` | `year` (int), `state_code` (str), `table_code` (str), `geo_level` (str), `place_code` (opt), `county_code` (opt) | Queries live Census API with formatted headers and returns JSON records. |
| `batch_download` | `state_code`, `table_code`, `start_year`, `end_year`, `geo_level`, `place_codes` / `county_codes` / `zcta_codes` / `msa_codes` (opt), `output_format`, `output_dir` (opt), `dataset`, `clean_sentinels`, `compute_reliability`, `inflation_base_year`, `include_codebook`, `use_cache`, `slug_style` | Batch-downloads multi-year data to wide/melted CSVs, Excel, a tidy `(GEOID, Year)` panel CSV, or Parquet, with optional codebook and extraction metadata. |
| `list_counties` | `state_code` (str), `year` (int) | Lists counties with 3-digit FIPS and 5-digit GEOIDs. |
| `list_tracts` | `state_code` (str), `county_code` (str), `year` (int) | Lists all Census tracts in a county with 11-digit GEOIDs. |
| `list_zctas` | `state_code` (opt), `county_code` (opt), `zip_code` (opt) | Lists ZCTAs in a state/county, or resolves a ZIP code to its county via `zip2fips.json`. |
| `query_data_profile` | `profile_code` (`DP02`–`DP05`), `year`, `state_code`, `geo_level`, entity filters, `compute_reliability` | Queries the Data Profile endpoint (pre-computed percentages `*PE` with MOEs `*PM`). |
| `calculate_cv` | `estimate`, `moe` | Standard error (MOE/1.645), CV %, reliability flag (High ≤15%, Moderate ≤30%, Low >30%), 90% bounds. |
| `calculate_significance` | `est1`, `moe1`, `est2`, `moe2`, `confidence_level`, `year1`/`year2` (opt) | Two-tailed Z-test with p-value and verdict; applies the Census overlapping-period adjustment when years are given. |
| `adjust_inflation` | `value`, `from_year`, `to_year` | Converts dollars between years using the BLS CPI-U annual average. |
| `generate_codebook` | `table_code`, `year`, `dataset`, `include_moe`, `include_reliability`, `output_path` (opt) | Emits a data dictionary (code, slug, measurement type, description, concept, universe, unit). |

**Geography levels** (`geo_level`): `us`, `state`, `county`, `tract`, `place`, `zcta`, `msa` (CBSA).
**Datasets** (inferred from the table code): `acs5` (B/C detailed), `profile` (DP), `subject` (S), `cprofile` (CP).
**Query cache:** responses are stored in `.cache/census_cache.db` (override with `CENSUS_CACHE_PATH`; disable with `CENSUS_CACHE_DISABLED=1`).

---

## 2. Configuration for Claude Code (`claude` CLI)

Anthropic's **Claude Code** supports MCP servers either via CLI registration or via configuration files.

### Option A: One-line CLI Command
Run from within the repository root:
```bash
claude mcp add census -- python mcp_server.py
```

Or with an absolute path:
```bash
claude mcp add census -- python c:\Users\hobo\Desktop\US-Census-5-Year-ACS-Data-Batch-Downloader\mcp_server.py
```

### Option B: Project Configuration (`.claude/config.json`)
Create or edit `.claude/config.json` in the workspace root:

```json
{
  "mcpServers": {
    "census": {
      "command": "python",
      "args": [
        "c:\\Users\\hobo\\Desktop\\US-Census-5-Year-ACS-Data-Batch-Downloader\\mcp_server.py"
      ],
      "env": {
        "CENSUS_API_KEY": "${CENSUS_API_KEY}"
      }
    }
  }
}
```

### Option C: User-Global Configuration (`~/.claude.json`)
To enable the tool across all projects in Claude Code, add the server under `"mcpServers"` in `~/.claude.json` (located at `%USERPROFILE%\.claude.json` on Windows):

```json
{
  "mcpServers": {
    "census": {
      "command": "python",
      "args": [
        "c:\\Users\\hobo\\Desktop\\US-Census-5-Year-ACS-Data-Batch-Downloader\\mcp_server.py"
      ]
    }
  }
}
```

---

## 3. Configuration for Claude Desktop

On Windows, open or create the configuration file at:
`%APPDATA%\Claude\claude_desktop_config.json`
*(Full path: `C:\Users\<Username>\AppData\Roaming\Claude\claude_desktop_config.json`)*

Add the `census` entry under `"mcpServers"`:

```json
{
  "mcpServers": {
    "census": {
      "command": "python",
      "args": [
        "c:\\Users\\hobo\\Desktop\\US-Census-5-Year-ACS-Data-Batch-Downloader\\mcp_server.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

> **Note:** If `python` is not in your system `PATH`, specify the full path to your Python executable (e.g. `C:\\Python312\\python.exe`).

Restart Claude Desktop. The hammer/tools icon will display the Census tools (`list_states`, `search_tables`, `fetch_table_data`, etc.).

---

## 4. Example Prompts for Claude

Once connected in Claude Code or Claude Desktop, you can ask questions directly:

- *"What tables are available for median household income in ACS 5-year data?"*
- *"Show me the variables inside table B19013."*
- *"Fetch population by age (table B01001) for New York city for 2021 and 2022."*
- *"Download table B01001 for Austin, Texas from 2018 to 2022 and export it as an Excel spreadsheet."*
- *"Download median household income (B19013) for every tract in Travis County, TX from 2015 to 2022 as Parquet, in constant 2022 dollars, with CVs and a codebook."*
- *"Is the change in Austin's median household income between the 2017 and 2022 ACS statistically significant?"*
- *"Pull the DP03 economic profile for all counties in Ohio and flag estimates with low reliability."*
- *"Which ZCTAs are in Manhattan (county 36061)? Get B01003 total population for them."*

