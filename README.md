# Census Research Engine

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23925301.svg)](https://doi.org/10.5281/zenodo.23925301)

A Python toolkit for extracting American Community Survey (ACS) 5-year data from the US Census Bureau API into research-ready files. It handles multi-year, multi-geography batches. It keeps estimates paired with their margins of error and derives proportions, ratios, sums, medians for custom areas, and significance tests with multiple-comparison correction, using Census replicate weights for exact variances where Census publishes them. It converts dollar values to constant dollars with the Census Bureau's index (R-CPI-U-RS), harmonizes tract panels across the 2010/2020 boundary change, and writes tidy panels, Parquet files, codebooks, and replayable provenance metadata.

Calculations are checked against values the Census Bureau publishes itself (profile percentages, comparison-profile inflation adjustments and significance flags, replicate-based MOEs, medians). See [Validation](#validation-against-census-published-values).

Multi-year panels are built from **concepts**, not raw variable codes. ACS codes are renumbered, reused, and redefined between releases, so every (year, code) is matched through a checked concordance, and anything unverifiable stops the run instead of silently mixing measures. See [Cross-release concordance](#cross-release-concordance).

| Interface | Entry point | Intended user |
| :--- | :--- | :--- |
| MCP server | `mcp_server.py` (57 tools; `--profile core` gives 14 for local models) | Agents in Claude Code, Claude Desktop, other MCP clients |
| Command line | `python -m census <command> --json` | Agents and scripts without MCP |
| Python library | `census` package (`plan_extraction`, `run_batch`) | Notebooks, pipelines |
| Desktop GUI | `acs_downloader.py` (Tkinter) | Legacy point-and-click use; maintenance mode |

All four interfaces share one pipeline and the same defaults.

## Installation

Tested on Python 3.10–3.12 (Linux and Windows CI).

```bash
git clone https://github.com/cafeTechne/Census-Research-Engine
cd Census-Research-Engine
pip install -r requirements.txt
```

Or install it as an editable package from the clone (`pip install -e .`, or `pip install -e ".[dev]"` for pytest and ruff; the toolkit reads its metadata archive and data files from the repository), which adds the `census-acs` command (same as `python -m census`) and `census-acs-mcp` (the MCP server; `census-acs-mcp --profile core`). Releases are listed in `CHANGELOG.md`; each tagged release is archived on Zenodo with a DOI to cite.

Price-index data (R-CPI-U-RS, CPI-U, and C-CPI-U, 2000–2024) ships with the repository, so constant-dollar conversion works out of the box.

**Refreshing R-CPI-U-RS** (once a year, after BLS publishes a new annual average). BLS publishes the series only as an Excel file and blocks anonymous scripted downloads. Download `https://www.bls.gov/cpi/research-series/r-cpi-u-rs-allitems.xlsx` in a browser, save it over `data/price_indexes/source/r-cpi-u-rs-allitems.xlsx`, and run:

```bash
python scripts/update_price_indexes.py --r-cpi-u-rs-xlsx data/price_indexes/source/r-cpi-u-rs-allitems.xlsx
```

Alternatively, set `BLS_USER_AGENT` to an identifying User-Agent that includes a contact email (BLS's requirement for automated clients) and run the script without arguments. A year the index does not cover fails with `PRICE_INDEX_UNAVAILABLE`.

## Configuration

| Variable | Purpose |
| :--- | :--- |
| `CENSUS_API_KEY` | Census API key ([request one](https://api.census.gov/data/key_signup.html)); requests without a key are rate-limited |
| `CENSUS_CACHE_PATH` / `CENSUS_CACHE_DISABLED=1` | SQLite query cache location (default `.cache/census_cache.db`), or disable it |
| `CENSUS_METADATA_DIR` | Alternate per-vintage metadata archive (default `metadata/`) |
| `CENSUS_CROSSWALK_DIR` | Alternate override directory (default `crosswalks/`) |
| `CENSUS_PRICE_INDEX_DIR` | Alternate price-index directory (default `data/price_indexes/`); not checksum-verified, its CSV's SHA-256 is recorded instead |
| `CENSUS_PRICE_PARITY_DIR` | Alternate Regional Price Parity directory (default `data/price_parities/`); same treatment |
| `BLS_USER_AGENT` | Identifying User-Agent for `scripts/update_price_indexes.py` |
| `CENSUS_VRE_DIR` | Cache for downloaded Census Variance Replicate Estimate files (default `.cache/vre/`) |
| `CENSUS_WWW2_MIN_INTERVAL` / `CENSUS_WWW2_ATTEMPTS` / `CENSUS_WWW2_BACKOFF_BASE` / `CENSUS_WWW2_REJECTED_RETRY_DELAY` | census.gov file downloads (`census/http.py`): seconds between requests to one host (default 1), attempts on HTTP 429/5xx or connection errors (4), backoff base in seconds (2; doubled per retry, capped at 60, honours `Retry-After`), and the wait before the single retry of a firewall "Request Rejected" page (30). A rejection that persists names the cache path where a browser-downloaded copy of the file is picked up |
| `CENSUS_LIVE_TESTS=1` | Enable live API contract tests and the live validation battery |

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
python -m census extract --table B19013 --geo county --state all --years 2022 --format parquet --out out
python -m census replay out/B19013_extraction_metadata.json --json
python -m census derive proportion --estimates 4500,30000 --moes 400,900 --json
python -m census aggregate --input out/B17001_tract_panel_2023.csv --groups groups.csv --year 2023 \
    --derive '[{"name": "poverty_rate", "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}]'
python -m census validate --offline
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
7. **Table-scoped rules.** `crosswalks/<dataset>/<table>_rules.csv` holds reviewed regex rewrites for systematic redesigns (two rules resolve 165 of the 171 S1701 lines changed in 2017). Rules that would change any number in a label are rejected, and merged concepts are still continuity-checked. See [docs/rule_overrides.md](docs/rule_overrides.md).

Each multi-year run writes `<table>_concordance.csv` (every output column traced to its code in each year) and `<table>_concordance_issues.json`.

## Concepts

### Geography levels

| `geo_level` | Summary level | `GEOID` | `codes` in `build_entities` |
| :--- | :--- | :--- | :--- |
| `us` | 010 | `US` | none |
| `state` | 040 | `SS` | none; `state_code` required (`"all"` = 50 states + DC everywhere) |
| `county` | 050 | `SSCCC` | county FIPS; default all in state |
| `tract` | 140 | `SSCCCTTTTTT` | county FIPS; one request per county; default all |
| `place` | 160 | `SSPPPPP` | place FIPS (required) |
| `zcta` | 860 | `ZZZZZ` | ZCTAs; default all ZIPs in the state (`zip2fips.json`); requested in chunks of 50 |
| `msa` | 310 | `MMMMM` | CBSA codes; default all metro and micro areas |
| `block` | 100 | `SSCCCTTTTTTBBBB` | 2020 Census only (`dec/pl`, or DHC `H1`): county FIPS; one request per county, default one per state |

### Datasets

| `dataset` | Endpoint | Tables | Variable suffixes |
| :--- | :--- | :--- | :--- |
| `acs5` | `/acs/acs5` | `B…`, `C…` | `E`, `M`, `EA`/`MA` annotations |
| `profile` | `/acs/acs5/profile` | `DP02`–`DP05` | adds `PE`/`PM` percentages |
| `subject` | `/acs/acs5/subject` | `S…` | `E`, `M` per column (`_C01_`, …) |
| `cprofile` | `/acs/acs5/cprofile` | `CP02`–`CP05` | codes embed the period; `SS` significance flags |
| `dec/pl` | `/2020/dec/pl` | 2020 Census P.L. 94-171 redistricting data: `P1`–`P5`, `H1` | `N` (count), `NA` annotation; no MOE |
| `dec/dhc` | `/2020/dec/dhc` | 2020 Census Demographic and Housing Characteristics: e.g. `P1`, `P12`, `H1`, `H3` | `N` (count), `NA` annotation; no MOE |

The ACS dataset is inferred from the table code. The 2020 Census datasets are selected with `dataset="dec/pl"` / `"dec/dhc"` or `product="decennial"` (DHC); see [2020 Decennial Census](#2020-decennial-census).

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
| `<base>N`, `*_count` | 2020 Census count (and its panel slug); no MOE, SE or CV columns exist |

## Methodology

| Operation | Rule | Function |
| :--- | :--- | :--- |
| Standard error | `SE = MOE / 1.645` | `census.stats.standard_error` |
| Coefficient of variation | `CV = SE / estimate × 100`; High ≤ 15%, Moderate ≤ 30%, Low > 30% | `census.stats.coefficient_of_variation` |
| Difference test | `Z = |X1 − X2| / sqrt(SE1² + SE2²)`, scaled by `sqrt(1 − overlap/5)` for overlapping periods | `census.stats.z_test` |
| Sum of estimates | `MOE = sqrt(Σ MOE²)` over every component, including zeros (Handbook 2020 ch. 8; the older largest-zero-MOE rule is available) | `census.derived.sum_estimates` |
| Proportion (subset) | `MOE = sqrt(MOE_X² − P²·MOE_Y²) / Y`; ratio formula if the radicand is negative | `census.derived.proportion` |
| Ratio, product, percent change | Handbook 2020 ch. 8 formulas | `census.derived` |
| Exact variance | `Var = 4/80 · Σ (rep_r − est)²` over Census's 80 replicate estimates | `census.replicate`, `census.variance` |
| Median for custom areas | Sum bracket counts, linear interpolation (ACS Subject Definitions); MOE by replicates or an approximation | `census.medians` |
| Custom geographies | Sum counts, then derive; non-additive measures refused | `census.aggregate` |
| Multiple comparisons | Holm (default), Bonferroni, Benjamini–Hochberg, Benjamini–Yekutieli | `census.multitest` |
| Tract harmonization | `Σ w·X`, `MOE = sqrt(Σ (w·MOE)²)` with Census 2010/2020 tract relationship weights | `census.geo_crosswalk` |
| Sentinel values | Census jam values (`-999999999`, `-666666666`, …) become null | `census.stats.clean_census_sentinels` |
| Constant dollars | `real = nominal × index_base / index_year` on dollar-valued estimates and MOEs (not counts in dollar brackets); default R-CPI-U-RS, optional CPI-U and C-CPI-U | `census.inflation.deflate_dataframe` |

Price indexes live in `data/price_indexes/` and are generated from BLS sources by `scripts/update_price_indexes.py`, with checksums. Each bundled CSV (and each Regional Price Parity CSV in `data/price_parities/`) is checked against the `csv_sha256` in its sidecar JSON the first time it is loaded in a process; a mismatch raises `BundledDataIntegrityError` naming the file and both hashes. Tables from `CENSUS_PRICE_INDEX_DIR` or `CENSUS_PRICE_PARITY_DIR` are not checked; their SHA-256 is recorded in `price_index_info()["custom_table"]` (and so in the extraction metadata) or in the RPP adjustment notes. The sidecar JSON files themselves (including provisional values) are not hashed. R-CPI-U-RS values are the workbook's annual averages; the source workbook is kept in `data/price_indexes/source/` and its SHA-256 is recorded. CPI-U annual averages are computed from monthly values (they match BLS's published averages to ±0.001). 2025 is omitted because BLS did not publish October 2025; the R-CPI-U-RS workbook's 11-month 2025 average is stored as provisional and used only with `allow_provisional_index=True`, which adds an advisory. The Census Bureau switched its CPS income reports to C-CPI-U in 2023 but still uses R-CPI-U-RS for all ACS years.

Every derived result records its `method` (for example `proportion`, `proportion_ratio_fallback`, `replicate`, `median_interpolated`, `apportioned`) alongside `estimate`, `moe`, `se`, and `cv_percent`. Approximation formulas ignore covariance; for estimates that feed a hypothesis test, check `check_replicate_availability` and prefer replicate variances.

## Validation against Census-published values

`python scripts/validate_against_census.py` (or `python -m census validate`, or the MCP tool `run_validation`) recomputes numbers the Census Bureau publishes independently and compares them with this toolkit's output. Every check runs offline against frozen fixtures in the normal test suite, and CI runs the live battery weekly. The latest report is [docs/audits/validation_report.md](docs/audits/validation_report.md).

| Check | Recomputed by the toolkit | Census-published value |
| :--- | :--- | :--- |
| F1 | Pipeline output cells (panel, Parquet) | Raw API cells |
| F2 | Proportions and MOEs from detailed tables | Data Profile percentages and percent MOEs |
| F3 | MOEs from 80 replicate estimates | Published MOEs |
| F4 | Exact replicate proportion MOEs | Data Profile percent MOEs |
| F5 | Medians from distribution tables | Published medians |
| F6 | Prior-period dollars adjusted with R-CPI-U-RS | Comparison Profile values in current dollars |
| F7 | Z-tests between periods | Comparison Profile significance flags |
| F8 | Sums of counties | State estimates |
| F11 | 2020 Census state population; block housing units summed to tracts and counties; DHC vs P.L. 94-171 | Apportionment-release state totals; published tract and county totals (invariants and hierarchy) |

## Inference

Models and figures that keep ACS sampling error in view, validated by simulation ([docs/audits/inference_validation.md](docs/audits/inference_validation.md)):

| Capability | Function / tool | Validation |
| :--- | :--- | :--- |
| Regression with replicate sampling SEs | `census.models.fit_replicate` / `fit_model` | 90% intervals cover 0.896 (population target) and 0.895 (these-areas target) |
| Noisy predictors | `census.models.regression_calibration` | Attenuation removed; coverage 0.89 |
| Small-area estimation (Fay–Herriot) | `census.sae.fay_herriot` / `small_area_estimates` | Reproduces the published R `sae` milk example; EBLUP coverage 0.897 |
| Correlations with intervals and replicate SEs | `census.correlation.correlation_matrix` | Coverage 0.895 |
| Trend tests on non-overlapping releases | `census.trends.linear_trend_test` / `trend_test` | Size 0.10 at nominal 0.10 |
| Minimum detectable differences, reliability profiles | `census.planning` | |
| Journal figures and LaTeX/Markdown tables | `census.figures`, `census.report_tables` | Byte-identical regeneration |
| Pre-registered analyses | `census.manifest` / `create_manifest`, `run_manifest` | Hash in every output; deviations recorded |

SIMEX is available but under-covers (0.81) and is not recommended for confirmatory use. Statistics use `scipy` and `statsmodels`; figures use `matplotlib`.

**Local models.** `python mcp_server.py --profile core` serves a 13-tool profile with short descriptions, `summary`/`next_step` in every result, and step-by-step recipes (`get_recipe`). Qwen3 8B on CPU completed 5 of 6 evaluation tasks ([docs/audits/local_model_eval.md](docs/audits/local_model_eval.md)); setup in [docs/local_models.md](docs/local_models.md).

## Geography, maps, and spatial analysis

Built on GeoPandas, Shapely (GEOS), pyogrio (GDAL), pyproj, and PySAL. Outputs are GeoPackage by default, which ArcGIS Pro and QGIS open natively.

| Capability | Function / tool | Notes |
| :--- | :--- | :--- |
| Census boundaries matched to each ACS release | `census.boundaries.get_boundaries` / `get_boundaries` | Cartographic (1:500k) for maps, TIGER/Line for areal work; vintage mismatches are errors |
| Journal maps that show uncertainty | `census.maps.choropleth` / `make_map` | Quantile classes by default; legend reports class separability (Sun and Wong 2010); low-reliability areas hatched; uncertainty and significance maps |
| Spatial autocorrelation | `census.spatial.morans_i`, `local_morans` / `spatial_autocorrelation` | Reproduces PySAL references; replicate SE of Moran's I is conservative (coverage 0.987); local clusters use two-sided permutation p-values (9999 permutations, BH): false-positive rates at nominal under randomness |
| Spatial regression | `census.spatial.spatial_regression` / `spatial_regression` | Lag (GMM default) and error (ML default) models with LM diagnostics (spreg); simulated coverage under ACS outcome error: rho 0.89-0.90 (GMM lag), lambda attenuated (0.003-0.85). Error model option `correct_outcome_error=True, se_col=...` corrects lambda for the outcome's sampling error (spatial Fay-Herriot likelihood, REML): lambda coverage 0.86-0.93. It assumes independent errors across areas, which ACS replicates support for rates, shares and medians ([covariance audit](docs/audits/acs_sampling_covariance.md)); it warns for population/household totals and is biased by positively correlated errors ([validation](docs/audits/spatial_regression_validation.md)) |
| Spatial Fay–Herriot | `census.sae_spatial.spatial_fay_herriot` / `spatial_small_area_estimates` | Reproduces the R `sae` grapes example; coverage 0.891 |
| Population-weighted tract crosswalks | `census.block_weights`, `harmonize_panel(weight="auto")` | Decennial block weights; Delaware test: mean error 444 vs 624 people with land-area weights |
| Block groups and custom polygons | `geo_level="block group"`, `census.custom_areas` / `custom_area_mapping` | Area or block-population allocation |
| ACS errata and user notes | `census.errata.errata_for`; in every extraction plan | 258 notes from census.gov with sources |

## Microdata, 1-year estimates, and research designs

| Capability | Function / tool | Validation |
| :--- | :--- | :--- |
| PUMS custom tabulations (count, mean, proportion, median, ratio, crosstabs) | `census.pums.tabulate` / `pums_tabulate` | Reproduces all 32 Census PUMS verification estimates and SEs (Wyoming, 2024 1-year and 2020–2024 5-year) |
| PUMS regression with replicate weights | `census.pums.pums_regression` / `pums_regression` | Design-based SEs from the 80 replicate weights |
| ACS 1-year estimates | `product="1-year"` in `plan_extraction`, `batch_download`, `run_batch` | Areas of 65,000+ only (others reported); 2020 skipped (no standard release); 1-year vs 5-year comparisons refused |
| Multilevel models | `census.multilevel.fit_multilevel` / `multilevel_model` | Kenward–Roger intervals; coverage 0.89; reproduces the statsmodels `dietox` reference |
| Difference-in-differences | `census.did.difference_in_differences` | Non-overlapping periods only; coverage 0.90–0.91; reproduces Card and Krueger (1994) |
| Cost of living between places | `census.rpp.adjust_for_place` / `adjust_for_place` | BEA Regional Price Parities; reproduces BEA real personal income for all states (2008); never applied by default |
| Interactive maps | `census.web_maps.web_choropleth` / `make_web_map` | Self-contained HTML, no external requests |

## 2020 Decennial Census

The 2020 Census P.L. 94-171 redistricting data (`dec/pl`) and the Demographic and Housing Characteristics File (`dec/dhc`) use the same client, cache, extraction pipeline, output formats, codebook and replay as the ACS: pass `dataset="dec/dhc"` (or `"dec/pl"`), or `product="decennial"`, to `plan_extraction`, `batch_download`, `download_table`, `run_batch` or `python -m census extract --product decennial`, with `years=[2020]`. Geography levels are nation, state, county, tract, block group, place and block (block only for P.L. 94-171 and the invariant DHC `H1` housing-unit count). Variable metadata for both files is archived in `metadata/dec/`.

```python
from census import ResearchOptions, build_entities, run_batch

run_batch("P12", build_entities("county", "26"), [2020], "out", "panel",
          ResearchOptions(include_codebook=True), dataset="dec/dhc")
```

**Uncertainty.** Census counts are not sample-based, so they have no sampling margin of error; they are also not error-free. Outputs never carry an MOE or CV of 0: no MOE, SE, CV or reliability columns are written (`compute_reliability` adds none), the codebook's `Uncertainty` column reads "not sample-based (MOE undefined)", and the extraction metadata has `moe_confidence_level: null` and an `uncertainty` record. Every decennial plan carries a disclosure-avoidance advisory:

- the counts carry coverage error (measured by the [Post-Enumeration Survey](https://www.census.gov/newsroom/blogs/random-samplings/2021/12/post-enumeration-measuring-coverage-error.html)), processing error, and noise added by the TopDown differential-privacy algorithm;
- only three counts are invariant (published without noise): state total population, total housing units per block, and the number of occupied group-quarters facilities by type per block ([Census DAS update, 2020-11-25](https://www.census.gov/programs-surveys/decennial-census/decade/2020/planning-management/process/disclosure-avoidance/2020-das-updates/2020-11-25.html));
- small geographies and detailed cells are noisier, and block-level characteristics should be aggregated before use ([fact sheet](https://www.census.gov/content/dam/Census/library/factsheets/2021/protecting-the-confidentiality-of-the-2020-census-redistricting-data.pdf)).

**Statistics.** `z_test`, `calculate_significance`, `batch_significance`, `calculate_cv` and `acs_overlap_years` refuse decennial-only inputs (`product="decennial"`), since a sampling test says nothing about disclosure-avoidance noise. An ACS estimate can be tested against a census count with `product1="5-year", product2="decennial"`: the count is a fixed reference, and the result's `comparison` and `notes` say that only ACS sampling error is measured. `derive_estimate(..., product="decennial")` returns the point estimate with MOE, SE and CV undefined. `build_analysis_frame` gives census counts NaN `_se`/`_cv`, method `not_sample_based`, release `dec_2020` (`acs5_<year>+dec_2020` with a note when mixed with ACS variables). No new estimators are provided for decennial data; validation check F11 compares the files with the Census Bureau's invariants and hierarchy.

## Advisories and known limitations

- **Boundary vintages.** Requests spanning 2019 to 2020 at tract, ZCTA, place, county, or CBSA level produce an advisory. Tract panels can be harmonized with `harmonize_tracts_to=2010|2020` (land-area weights from the Census relationship file, or NHGIS-style population weights you supply); apportioned MOEs assume independent components and exact weights, and boundary slivers produce tiny weights. Other levels are not harmonized.
- **Medians from coarse brackets.** Household-income medians rebuilt from B19001 are usually within 1% of the published B19013, but can differ by more than 2% because Census interpolates a finer internal distribution.
- **Home values.** Before the 2022 releases, home values were not on the period end-year dollar basis (ACS user note 2023-07), so deflating them is approximate. Plans include an advisory.
- **Subject tables.** Their 2013 and 2017 redesigns changed thousands of labels. Multi-year subject pulls usually need `variables=` and reviewed overrides.
- **2020 Census counts.** No margins of error exist; disclosure-avoidance noise dominates blocks and detailed cells. Aggregate before use; see [2020 Decennial Census](#2020-decennial-census). Cross-release concordance and continuity checks do not apply (one census year), and the 2010 Census is not offered.
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
  variance.py      DerivedEstimate and replicate-variance primitives
  derived.py       Proportions, ratios, products, sums, percent change (Handbook ch. 8)
  replicate.py     Census Variance Replicate Estimate tables: download, parse, exact variances
  medians.py       Medians and MOEs from distribution tables
  aggregate.py     Custom geography aggregation
  multitest.py     Multiple-comparison corrections, batch significance tests
  geo_crosswalk.py 2010/2020 tract apportionment, Connecticut recodes
  validation.py    Checks against Census-published values
  workflows.py     File-level workflows shared by the MCP server and CLI
  panel.py         Slugs, codebooks, tidy panels, Parquet
  geo.py, tables.py, export.py, errors.py, __main__.py (CLI)
mcp_server.py      MCP server (instructions + 32 tools)
acs_downloader.py  Tkinter GUI (maintenance mode)
metadata/          Per-vintage ACS metadata archive with manifest
crosswalks/        Reviewed concordance overrides and their evidence
data/price_indexes/  R-CPI-U-RS, CPI-U, and C-CPI-U with source sidecars
data/geo_crosswalks/ Census 2010/2020 tract relationships and Connecticut recodes
scripts/           fetch_metadata_archive.py, update_price_indexes.py, audit_concordance.py,
                   fetch_tract_relationships.py, validate_against_census.py
docs/              agent_protocol.md, claude_setup.md, audits/
tests/             unittest suite (pytest); tests/test_live.py is opt-in
```

## Testing

```bash
python -m pytest -q                                          # offline, including validation fixtures
CENSUS_LIVE_TESTS=1 python -m pytest -q                      # adds live API and validation tests
python scripts/validate_against_census.py --live             # full live validation report
python -m census validate-crosswalks
```

## Citation and attribution

Created and maintained by [cafeTechne](https://github.com/cafeTechne). If you use this toolkit in research, cite it using [CITATION.cff](CITATION.cff) (GitHub's "Cite this repository" button produces APA and BibTeX) or the concept DOI [10.5281/zenodo.23925301](https://doi.org/10.5281/zenodo.23925301), which always resolves to the latest release (each release also has its own version DOI on Zenodo), and cite the Census Bureau as the source of the data, naming the ACS 5-year release(s) and table(s) used. Forks and derivative works must keep this attribution and the license notice.

## License

Copyright (C) 2023–2026 cafeTechne.

Licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE). Contributions require the [Contributor License Agreement](CLA.md); see [CONTRIBUTING.md](CONTRIBUTING.md).
