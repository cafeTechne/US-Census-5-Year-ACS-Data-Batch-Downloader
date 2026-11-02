# US Census ACS 5-Year Data Batch Downloader

A Python toolkit for extracting American Community Survey (ACS) 5-year data from the US Census Bureau API into research-ready files. It handles multi-year, multi-geography batches. It keeps estimates paired with their margins of error, derives standard errors, coefficients of variation, and significance tests, and converts dollar values to constant dollars with the Census Bureau's index (R-CPI-U-RS). It also writes tidy panels, Parquet files, codebooks, and provenance metadata.

Multi-year panels are built from **concepts**, not raw variable codes. ACS codes are renumbered, reused, and redefined between releases, so every (year, code) is matched through a checked concordance, and anything unverifiable stops the run instead of silently mixing measures. See [Cross-release concordance](#cross-release-concordance).

| Interface | Entry point | Intended user |
| :--- | :--- | :--- |
| MCP server | `mcp_server.py` (22 tools, with server instructions) | Agents in Claude Code, Claude Desktop, other MCP clients |
| Command line | `python -m census <command> --json` | Agents and scripts without MCP |
| Python library | `census` package (`plan_extraction`, `run_batch`) | Notebooks, pipelines |
| Desktop GUI | `acs_downloader.py` (Tkinter) | Legacy point-and-click use; maintenance mode |

All four interfaces share one pipeline and the same defaults.

## Installation

Tested on Python 3.12.

```bash
git clone https://github.com/cafeTechne/US-Census-5-Year-ACS-Data-Batch-Downloader
cd US-Census-5-Year-ACS-Data-Batch-Downloader
pip install -r requirements.txt
```

**One-time step for constant-dollar conversion.** R-CPI-U-RS is published by BLS only as an Excel file, and BLS blocks anonymous scripted downloads. Download `https://www.bls.gov/cpi/research-series/r-cpi-u-rs-allitems.xlsx` in a browser, then run:

```bash
python scripts/update_price_indexes.py --r-cpi-u-rs-xlsx path/to/r-cpi-u-rs-allitems.xlsx
```

Alternatively, set `BLS_USER_AGENT` to an identifying User-Agent that includes a contact email (BLS's requirement for automated clients) and run the script without arguments. Until this is done, constant-dollar requests fail with `PRICE_INDEX_UNAVAILABLE`, unless `price_index="cpi-u"` is chosen.

## Configuration

| Variable | Purpose |
| :--- | :--- |
| `CENSUS_API_KEY` | Census API key ([request one](https://api.census.gov/data/key_signup.html)); requests without a key are rate-limited |
| `CENSUS_CACHE_PATH` / `CENSUS_CACHE_DISABLED=1` | SQLite query cache location (default `.cache/census_cache.db`), or disable it |
| `CENSUS_METADATA_DIR` | Alternate per-vintage metadata archive (default `metadata/`) |
| `CENSUS_CROSSWALK_DIR` | Alternate override directory (default `crosswalks/`) |
| `CENSUS_PRICE_INDEX_DIR` | Alternate price-index directory (default `data/price_indexes/`) |
| `BLS_USER_AGENT` | Identifying User-Agent for `scripts/update_price_indexes.py` |
| `CENSUS_LIVE_TESTS=1` | Enable live API contract tests |

## Quick start

### Python

```python
from census import ResearchOptions, build_entities, plan_extraction, run_batch

entities = build_entities("tract", "48", ["453"])       # all tracts in Travis County, TX
options = ResearchOptions(compute_reliability=True, inflation_base_year=2022, include_codebook=True)

plan = plan_extraction("B19013", entities, range(2015, 2023), "parquet", options,
                       variables=["B19013_001"])        # name the variables you analyze
print(plan["would_block"], plan["selection"], plan["issues"][:3])

result = run_batch("B19013", entities, range(2015, 2023), "out", "parquet", options,
                   variables=["B19013_001"])
print(result["success"], result.get("error_code"), result.get("panel_path"))
```

Results are dictionaries. Failures carry `success: False`, an `error_code`, and `error`; nothing raises on API failure.

### Command line

```bash
python -m census plan    --table DP02 --geo state --state 36 --years 2018-2022 --variables DP02_0016 --json
python -m census extract --table DP02 --geo state --state 36 --years 2018-2022 --variables DP02_0016 --out out --json
python -m census concordance --table B27001 --years 2016-2017 --variables B27001_006 --json
python -m census describe --code DP02_0002PE --year 2018
```

Exit codes: `0` success, `2` invalid input, `3` concordance review required, `4` API or data failure.

### MCP server

```bash
claude mcp add census -- python mcp_server.py
```

The server sends instructions that describe the extraction and review workflow, and `get_research_protocol` returns the full protocol. See [docs/claude_setup.md](docs/claude_setup.md) for client configuration and the tool list.

## Cross-release concordance

ACS variable meanings drift between releases. The offline audit ([docs/audits/concordance_audit.md](docs/audits/concordance_audit.md)) covers every profile, subject, and comparison-profile table for 2009–2024. It finds thousands of renumbered lines, reused codes, and changed definitions. Real examples:

| Case | Example |
| :--- | :--- |
| Renumbered | DP02 "average household size": `DP02_0015` (2018) became `DP02_0016` (2019) |
| Code reused | `DP02_0002`: "family households" (2018), then "married couple family" (2019) |
| Definition changed | `B27001_006`: "6 to 17 years" (2016), then "6 to 18 years" (2017) |

How it is handled:

1. **Per-vintage metadata.** `metadata/<dataset>/<year>/` holds every release's `variables.json` and `groups.json` (gzipped, SHA-256 in `metadata/manifest.json`), so labels and matching never depend on a single vintage.
2. **Concordance.** Labels are normalized by tested, versioned rules (measurement prefixes, the dollar year, punctuation, part order). Only exact matches merge automatically. Renumbered lines become one column, and reused codes become separate columns.
3. **Continuity check.** Every concept that spans years is compared at the national level. A change that is both significant and implausible blocks the run.
4. **Selection.** `variables=` names what the analysis uses. A bare code resolves in the latest requested year, `YEAR:CODE` pins a vintage, and concept IDs are exact.
5. **Modes.** `fail` (default) stops when unresolved issues touch selected variables, `strict` proceeds and reports, and `code` is the legacy raw-code mode.
6. **Reviewed overrides.** `crosswalks/<dataset>/<table>.csv` records `merge`, `split`, and `exclude` decisions, each drafted and then independently reviewed (by an agent or a human) with generated evidence and a census.gov source. The rules are in [docs/agent_protocol.md](docs/agent_protocol.md) and enforced in code.

Each multi-year run writes `<table>_concordance.csv` (every output column traced to its code in each year) and `<table>_concordance_issues.json`.

## Concepts

### Geography levels

| `geo_level` | Summary level | `GEOID` | `codes` in `build_entities` |
| :--- | :--- | :--- | :--- |
| `us` | 010 | `US` | none |
| `state` | 040 | `SS` | none; `state_code` required |
| `county` | 050 | `SSCCC` | county FIPS; default all in state |
| `tract` | 140 | `SSCCCTTTTTT` | county FIPS; one request per county; default all |
| `place` | 160 | `SSPPPPP` | place FIPS (required) |
| `zcta` | 860 | `ZZZZZ` | ZCTAs; default all ZIPs in the state (`zip2fips.json`); requested in chunks of 50 |
| `msa` | 310 | `MMMMM` | CBSA codes; default all metro and micro areas |

### Datasets

| `dataset` | Endpoint | Tables | Variable suffixes |
| :--- | :--- | :--- | :--- |
| `acs5` | `/acs/acs5` | `B…`, `C…` | `E`, `M`, `EA`/`MA` annotations |
| `profile` | `/acs/acs5/profile` | `DP02`–`DP05` | adds `PE`/`PM` percentages |
| `subject` | `/acs/acs5/subject` | `S…` | `E`, `M` per column (`_C01_`, …) |
| `cprofile` | `/acs/acs5/cprofile` | `CP02`–`CP05` | codes embed the period; `SS` significance flags |

The dataset is inferred from the table code.

### Outputs

| `output_format` | Files |
| :--- | :--- |
| `both`, `wide_only`, `melted_only` | Per entity folder: wide `compiled_unmelted_data.csv` and/or long per-year CSVs |
| `excel` | Per entity folder: workbook with `Wide_Data` and `Melted_Data` sheets |
| `panel` | `<table>_<geo>_panel_<span>.csv`, one row per `(geoid, year)` |
| `parquet` | The same panel as Parquet (snappy, native dtypes) |

Every run also writes `<table>_extraction_metadata.json`. It records the request, the package version and git commit, the metadata checksum per vintage, the concordance mode and normalization version, the overrides applied, the price index and its source, the advisories, and the failed calls. `include_codebook=True` adds `<table>_codebook.csv` (code, slug, measurement type, description, concept, universe, unit, concept ID, codes by year, concordance status).

### Column conventions

| Column | Meaning |
| :--- | :--- |
| `GEOID` / `geoid` | Standard Census GEOID (TIGER/Line join key) |
| `Year` / `year` | ACS 5-year period end year (2022 = 2018–2022) |
| `<base>E`, `<base>M` | Estimate and 90% margin of error; in panels, `<base>` is the concept's latest-year code |
| `<base>__<first>_<last>` | Concept segment when a code was reused for different measures |
| `<base>_SE`, `_CV`, `_RELIABILITY` | Derived standard error, CV (%), and reliability flag |
| `*_est`, `*_moe`, `*_pct`, `*_pct_moe`, `*_se`, `*_cv`, `*_rel`, `*_sig` | Panel slugs; `slug_style="code"` gives Stata-safe names |

## Methodology

| Operation | Rule | Function |
| :--- | :--- | :--- |
| Standard error | `SE = MOE / 1.645` | `census.stats.standard_error` |
| Coefficient of variation | `CV = SE / estimate × 100`; High ≤ 15%, Moderate ≤ 30%, Low > 30% | `census.stats.coefficient_of_variation` |
| Difference test | `Z = |X1 − X2| / sqrt(SE1² + SE2²)`, scaled by `sqrt(1 − overlap/5)` for overlapping periods | `census.stats.z_test` |
| Sum of estimates | `MOE = sqrt(Σ MOE²)`; largest MOE once for zero components | `census.stats.aggregate_estimates` |
| Sentinel values | Census jam values (`-999999999`, `-666666666`, …) become null | `census.stats.clean_census_sentinels` |
| Constant dollars | `real = nominal × index_base / index_year` on dollar-valued estimates and MOEs (not counts in dollar brackets); default R-CPI-U-RS, optional CPI-U | `census.inflation.deflate_dataframe` |

Price indexes live in `data/price_indexes/` and are generated from BLS sources by `scripts/update_price_indexes.py`, with checksums. CPI-U annual averages are computed from monthly values (they match BLS's published averages to ±0.001). 2025 is omitted because BLS did not publish October 2025.

## Advisories and known limitations

- **Boundary vintages.** Requests spanning 2019 to 2020 at tract, ZCTA, place, county, or CBSA level produce an advisory. Tract panels across that break need a geographic crosswalk, which is not yet implemented.
- **Home values.** Before the 2022 releases, home values were not on the period end-year dollar basis (ACS user note 2023-07), so deflating them is approximate. Plans include an advisory.
- **Subject tables.** Their 2013 and 2017 redesigns changed thousands of labels. Multi-year subject pulls usually need `variables=` and reviewed overrides.
- **ZIP codes vs. ZCTAs.** `zip2fips.json` maps each ZIP to one primary county; PO-box-only ZIPs have no ZCTA.

## Repository layout

```
census/
  api.py           Census API client, geography predicates, dataset inference
  cache.py         SQLite response cache
  metadata.py      Per-vintage variable and table metadata (archive, then API)
  concordance.py   Label normalization, concordance, issues, override validation
  continuity.py    National-level continuity check
  review.py        Draft and record reviewed overrides
  pipeline.py      plan_extraction, run_batch, build_entities, ResearchOptions
  inflation.py     Price indexes, deflator, advisories
  stats.py         MOE, SE, CV, Z-tests, aggregation, sentinel cleaning
  panel.py         Slugs, codebooks, tidy panels, Parquet
  geo.py, tables.py, export.py, errors.py, __main__.py (CLI)
mcp_server.py      MCP server (instructions + 22 tools)
acs_downloader.py  Tkinter GUI (maintenance mode)
metadata/          Per-vintage ACS metadata archive with manifest
crosswalks/        Reviewed concordance overrides and their evidence
data/price_indexes/  R-CPI-U-RS and CPI-U with source sidecars
scripts/           fetch_metadata_archive.py, update_price_indexes.py, audit_concordance.py
docs/              agent_protocol.md, claude_setup.md, audits/
tests/             unittest suite (pytest); tests/test_live.py is opt-in
```

## Testing

```bash
python -m pytest -q                                          # offline
CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_live.py   # live API contracts
python -m census validate-crosswalks
```

## License

Copyright (C) 2023–2026 cafeTechne.

Licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE). Contributions require the [Contributor License Agreement](CLA.md); see [CONTRIBUTING.md](CONTRIBUTING.md).
