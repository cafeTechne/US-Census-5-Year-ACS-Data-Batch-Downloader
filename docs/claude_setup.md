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
| `batch_download` | `state_code` (str), `table_code` (str), `start_year` (int), `end_year` (int), `geo_level` (str), `place_codes` (opt), `output_format` (str), `output_dir` (opt) | Batch-downloads multi-year data and saves to wide/melted CSVs or multi-sheet Excel (.xlsx). |

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
