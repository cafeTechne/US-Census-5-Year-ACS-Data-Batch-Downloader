# US Census ACS 5-Year Data Batch Downloader

A Python toolkit for extracting American Community Survey (ACS) 5-year data from the US Census Bureau API into research-ready files. It handles multi-year, multi-geography batches; keeps estimates paired with their margins of error; derives standard errors, coefficients of variation, and significance tests; converts dollar values to constant dollars; and writes tidy panels, Parquet, codebooks, and provenance metadata.

The toolkit has three interfaces that share one pipeline:

| Interface | Entry point | Intended user |
| :--- | :--- | :--- |
| Python library | `census` package (`census.run_batch`) | Scripts, notebooks, agents with code execution |
| MCP server | `mcp_server.py` (14 tools) | Claude Code, Claude Desktop, other MCP clients |
| Desktop GUI | `acs_downloader.py` (Tkinter) | Legacy point-and-click use; in maintenance mode |

## Installation

Tested on Python 3.12.

```bash
git clone https://github.com/cafeTechne/US-Census-5-Year-ACS-Data-Batch-Downloader
cd US-Census-5-Year-ACS-Data-Batch-Downloader
pip install -r requirements.txt
```

Dependencies: `requests`, `pandas`, `openpyxl`, `python-dotenv`, `mcp`, `pyarrow`.

## Configuration

Settings are read from environment variables or a `.env` file in the repository root.

| Variable | Required | Purpose |
| :--- | :--- | :--- |
| `CENSUS_API_KEY` | Recommended | Census API key ([request one](https://api.census.gov/data/key_signup.html)). Requests without a key are rate-limited. |
| `CENSUS_CACHE_PATH` | No | SQLite query cache location. Default: `.cache/census_cache.db`. |
| `CENSUS_CACHE_DISABLED` | No | Set to `1` to bypass the query cache. |

## Quick start

### Python

```python
from census import ResearchOptions, build_entities, run_batch

result = run_batch(
    table_code="B19013",                              # median household income
    entities=build_entities("tract", "48", ["453"]),  # all tracts in Travis County, TX
    years=range(2015, 2023),
    output_dir="out",
    output_format="parquet",
    options=ResearchOptions(
        compute_reliability=True,   # add _SE, _CV, _RELIABILITY columns
        inflation_base_year=2022,   # constant 2022 dollars
        include_codebook=True,
    ),
)
print(result["panel_path"], result["codebook_path"], result["advisories"])
```

`run_batch` returns a dictionary with `success`, `successful_calls`, `failed_calls` (entity, year, error), `panel_path`, `codebook_path`, `metadata_path`, `advisories`, and per-entity results. It does not raise on API failures; check `success` and `failed_calls`.

### MCP server

```bash
claude mcp add census -- python mcp_server.py
```

See [docs/claude_setup.md](docs/claude_setup.md) for Claude Desktop configuration and the full tool reference.

### GUI

```bash
python acs_downloader.py
```

## Concepts

### Geography levels

| `geo_level` | Summary level | `GEOID` format | Selection (`codes` in `build_entities`) |
| :--- | :--- | :--- | :--- |
| `us` | 010 | `US` | none |
| `state` | 040 | `SS` | none; `state_code` required |
| `county` | 050 | `SSCCC` | county FIPS codes; default all in state |
| `tract` | 140 | `SSCCCTTTTTT` | county FIPS codes; one request per county; default all in state |
| `place` | 160 | `SSPPPPP` | place FIPS codes (required) |
| `zcta` | 860 | `ZZZZZ` | ZCTAs; default all ZIPs in the state from `zip2fips.json`; requested in chunks of 50 |
| `msa` | 310 | `MMMMM` (CBSA) | CBSA codes; default all metro and micro areas |

ZCTAs can only be filtered by state through the 2019 release; from 2020 the request is national and filtered to the listed ZCTAs.

### Datasets

The endpoint is inferred from the table code prefix and can be overridden with `dataset=`.

| `dataset` | Endpoint | Table codes | Variable suffixes |
| :--- | :--- | :--- | :--- |
| `acs5` | `/acs/acs5` | `B…`, `C…` | `E` estimate, `M` MOE, `EA`/`MA` annotations |
| `profile` | `/acs/acs5/profile` | `DP02`–`DP05` | adds `PE` percent, `PM` percent MOE |
| `subject` | `/acs/acs5/subject` | `S…` (e.g. `S1701`) | `E`, `M` per column (`_C01_`, `_C02_`, …) |
| `cprofile` | `/acs/acs5/cprofile` | `CP02`–`CP05` | codes embed the period; `SS` significance flags |

### Output formats

| `output_format` | Files written |
| :--- | :--- |
| `both` | Per entity folder: `compiled_unmelted_data.csv` (wide) and `<entity>_<table>_<year>.csv` (long) |
| `wide_only` / `melted_only` | One of the above |
| `excel` | Per entity folder: `<entity>_<table>_data.xlsx` with `Wide_Data` and `Melted_Data` sheets |
| `panel` | `<table>_<geo_level>_panel_<start>-<end>.csv`: one row per `(geoid, year)` |
| `parquet` | Same panel as Parquet (snappy compression, native dtypes, `geoid` stored as string) |

Every run also writes `<table>_extraction_metadata.json` (request parameters, endpoint, options, advisories, failed calls). With `include_codebook=True` it writes `<table>_codebook.csv` with columns `Variable_Code`, `Variable_Slug`, `Measurement_Type`, `Full_Description`, `Concept`, `Universe`, `Unit`.

### Column conventions

| Column | Meaning |
| :--- | :--- |
| `GEOID` / `geoid` | Standard Census GEOID (join key for TIGER/Line shapefiles) |
| `Year` / `year` | ACS 5-year period end year (2022 = 2018–2022) |
| `<base>E`, `<base>M` | Estimate and 90% margin of error (raw-code outputs) |
| `<base>_SE`, `<base>_CV`, `<base>_RELIABILITY` | Derived standard error, CV (%), and reliability flag |
| `<column>_NOTE` | Explanation of a replaced sentinel value or top/bottom coding |
| `*_est`, `*_moe`, `*_pct`, `*_pct_moe`, `*_se`, `*_cv`, `*_rel`, `*_sig` | Panel slugs; set `slug_style="code"` for Stata-safe names such as `b19013_001_est` |

## Methodology

| Operation | Formula or rule | Function |
| :--- | :--- | :--- |
| Standard error | `SE = MOE / 1.645` | `census.stats.standard_error` |
| Coefficient of variation | `CV = SE / estimate × 100` | `census.stats.coefficient_of_variation` |
| Reliability | High ≤ 15%, Moderate ≤ 30%, Low > 30% | `census.stats.reliability_flag` |
| Difference test | `Z = |X1 − X2| / sqrt(SE1² + SE2²)`, scaled by `sqrt(1 − overlap/5)` for overlapping 5-year periods | `census.stats.z_test` |
| Sum of estimates | `MOE = sqrt(Σ MOE²)`; for zero-valued components only the largest MOE is included | `census.stats.aggregate_estimates`, `aggregate_columns` |
| Sentinel values | `-999999999`, `-888888888`, `-666666666`, `-555555555`, `-333333333`, `-222222222` become null | `census.stats.clean_census_sentinels` |
| Constant dollars | `real = nominal × CPI_base / CPI_year`, applied to dollar-valued estimates and their MOEs (not to counts in dollar brackets) | `census.inflation.deflate_dataframe` |

The deflator currently uses BLS CPI-U annual averages for 2000–2024. Pass `cpi_table` to supply other years or another index.

## Advisories and known limitations

- **Boundary vintages.** Requests spanning 2019 to 2020 at tract, ZCTA, place, county, or CBSA level produce an advisory: 2020-and-later releases use 2020 Census geography. Tract panels across that break need a crosswalk before analysis.
- **Variable metadata vintage.** Detailed-table labels come from a single bundled `variables.json`. Profile, subject, and comparison-profile labels come from the API for the final requested year. Profile and subject line numbers are occasionally renumbered between releases, so pooled multi-year panels for those tables must be checked against per-year metadata.
- **Comparison profiles.** Variable codes embed the comparison period and differ between releases.
- **ZIP codes vs. ZCTAs.** `zip2fips.json` maps each ZIP to one primary county. PO-box-only ZIPs have no ZCTA and return no data.

## Repository layout

```
census/
  api.py        Census API client, geography predicates, dataset inference
  cache.py      SQLite response cache
  geo.py        FIPS reference data, GEOID construction, ZIP/ZCTA lookup
  tables.py     Table lists, variable metadata, universes
  stats.py      MOE, SE, CV, Z-tests, aggregation, sentinel cleaning
  inflation.py  CPI deflator and boundary advisories
  panel.py      Slugs, codebooks, tidy panels, Parquet
  export.py     Legacy CSV and Excel writers
  pipeline.py   run_batch, build_entities, ResearchOptions
mcp_server.py   MCP server
acs_downloader.py  Tkinter GUI
groups.json, variables.json  ACS table and variable metadata
zip2fips.json   ZIP to county crosswalk
state_places/   Census place definition files by state
tests/          unittest suite (run with pytest)
docs/           MCP setup guide
```

## Testing

```bash
python -m pytest -q
```

The suite mocks all network calls. Set `CENSUS_CACHE_DISABLED=1` when running ad hoc scripts that should not write to the local cache.

## License

No license file is included in this repository. Contact the author before reusing the code.
