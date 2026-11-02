# US Census ACS Batch Downloader — MCP Setup Guide

This guide details how to integrate the US Census ACS Data Batch Downloader with **Claude Code** and **Claude Desktop** via the Model Context Protocol (MCP).

---

## 1. Overview of MCP Tools

The server sends **instructions** to the client describing the extraction and review workflow. `get_research_protocol` returns the full protocol ([agent_protocol.md](agent_protocol.md)).

**Protocol and discovery**

| Tool | Key parameters | Purpose |
| :--- | :--- | :--- |
| `get_research_protocol` | none | Full agent protocol: workflow, concordance modes, issue kinds, draft/review rules |
| `list_states` | none | States and territories with FIPS codes |
| `list_vintages` | `dataset`, `refresh` | Release years per dataset (archive, optionally the live catalog) |
| `list_places` | `state_code`, `search`, `limit` | Places, cities, and CDPs in a state |
| `list_counties` | `state_code`, `year` | Counties with FIPS and GEOIDs |
| `list_tracts` | `state_code`, `county_code`, `year` | Tracts in a county with 11-digit GEOIDs |
| `list_zctas` | `state_code`, `county_code`, `zip_code` | ZCTAs in a state or county, or ZIP to county resolution |
| `search_tables` | `query`, `limit`, `year` | Detailed, profile, subject, and comparison tables by code or keyword |
| `list_variables` | `table_code`, `year`, `limit` | A table's variables and labels in one release |
| `describe_variable` | `code`, `year` | Label, concept, and universe of a code in one release |

**Data**

| Tool | Key parameters | Purpose |
| :--- | :--- | :--- |
| `fetch_table_data` | `year`, `state_code`, `table_code`, `geo_level`, entity codes, `compute_reliability` | One release, one geography, with that release's labels |
| `query_data_profile` | `profile_code`, `year`, `state_code`, `geo_level` | DP02–DP05 with percentages (`*PE`/`*PM`) |
| `plan_extraction` | same as `batch_download` | Dry run: calls, cached calls, `selection`, issues, `would_block`, advisories |
| `batch_download` | `state_code` (or `'all'`), `table_code`, `start_year`, `end_year`, `geo_level`, `variables`, entity codes, `output_format`, `concordance_mode`, `inflation_base_year`, `price_index`, `harmonize_tracts_to`, `include_codebook`, … | Multi-year extraction to CSV, Excel, tidy panel, or Parquet, with concordance, codebook, and metadata |
| `replay_extraction` | `metadata_path`, `verify`, `refresh` | Re-run an extraction from its metadata and compare every file and cell |

**Concordance review**

| Tool | Key parameters | Purpose |
| :--- | :--- | :--- |
| `check_variable_concordance` | `table_code`, `start_year`, `end_year`, `variables`, `include_nonblocking` | How variables line up across releases, with issues and evidence |
| `draft_crosswalk_override` | `table_code`, `entries` (`YEAR:CODE`), `decision`, `drafted_by`, `rationale`, `source_url`, `target_concept_id` | Step 1: generate evidence and draft rows |
| `record_crosswalk_review` | `row`, `reviewed_by`, `table_code` | Step 2: a different reviewer (agent or human) approves; validated before writing |
| `draft_rule_override` | `table_code`, `from_year`, `to_year`, `pattern`, `replacement`, `drafted_by`, `rationale`, `source_url` | Table-scoped rule for systematic redesigns: preview, guardrails, evidence |
| `record_rule_review` | `row`, `reviewed_by` | A different reviewer approves a rule; validated before writing |
| `validate_crosswalks` | none | Check all override and rule files against the review requirements |

**Statistics**

| Tool | Key parameters | Purpose |
| :--- | :--- | :--- |
| `calculate_cv` | `estimate`, `moe` | SE, CV %, reliability flag, 90% bounds |
| `calculate_significance` | `est1`, `moe1`, `est2`, `moe2`, `confidence_level`, `year1`/`year2` | Two-tailed Z-test with the overlapping-period adjustment |
| `adjust_inflation` | `value`, `from_year`, `to_year`, `index`, `allow_provisional` | Constant dollars with R-CPI-U-RS (default), CPI-U, or C-CPI-U |
| `generate_codebook` | `table_code`, `year`, `include_moe`, `include_reliability`, `output_path` | Data dictionary for one release |

**Derived statistics and geography**

| Tool | Key parameters | Purpose |
| :--- | :--- | :--- |
| `derive_estimate` | `operation`, `estimates`, `moes`, `as_percent` | Proportion, ratio, product, difference, percent change, or sum with its MOE and method |
| `check_replicate_availability` | `table_code`, `year`, `geo_level` | Whether Census publishes Variance Replicate Estimates (exact MOEs) |
| `aggregate_geographies` | `input_path`, `groups`, `variables`, `derive`, `year`, `use_replicates` | Combine geographies into custom areas, then derive rates |
| `median_from_distribution` | `table_code`, `year`, `input_path` or `counts`, `geoids`, `use_replicates` | Median and MOE for custom areas from distribution tables |
| `batch_significance` | `input_path`, `est1`, `moe1`, `est2`, `moe2`, `year1`, `year2`, `correction` | Many-geography difference tests with multiple-comparison correction |
| `apportion_tracts` | `input_path`, `target_vintage`, `weight`, `weights_file` | Harmonize a tract panel across the 2010/2020 tract change |
| `run_validation` | `checks`, `live` | Recompute Census-published values and report agreement |

Failed results carry `success: false`, an `error_code`, and `error`. **Geography levels:** `us`, `state`, `county`, `tract`, `place`, `zcta`, `msa`. **Datasets** (inferred): `acs5`, `profile`, `subject`, `cprofile`. **Query cache:** `.cache/census_cache.db` (`CENSUS_CACHE_PATH`, `CENSUS_CACHE_DISABLED=1`).

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
- *"Plan a 2015–2022 panel of DP02 average household size for all New York counties, review any concordance issues, and download it as Parquet."*
- *"Draft an override for the B27001 age-band change in 2017; then, in a separate session, review and record it."*
- *"Pull the DP03 economic profile for all counties in Ohio and flag estimates with low reliability."*
- *"Which ZCTAs are in Manhattan (county 36061)? Get B01003 total population for them."*

