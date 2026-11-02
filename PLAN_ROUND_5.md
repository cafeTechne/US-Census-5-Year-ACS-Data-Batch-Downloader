# PLAN_ROUND_5.md — US Census ACS Data Batch Downloader

**Planning Round:** 5  
**Date:** 2026-11-01  
**Target Milestone:** Research-Grade Econometric & Demographic Data Pipeline  
**Branch:** `phase5/research-grade-pipeline` (to branch off `defaultBranch`)  
**Status:** Planning Complete. Ready for Implementation Roadmap.

---

## Executive Overview

Phases 1 through 4 established a robust, freeze-free desktop GUI and headless Python library (`census/`) with an Anthropic FastMCP server (`mcp_server.py`) for Claude Code and Claude Desktop.

Phase 5 elevates this toolkit from a data fetcher to an **end-to-end econometric and demographic research engine**. It addresses fundamental scientific requirements for empirical research: statistical sampling error handling (MOEs, standard errors, coefficients of variation), high-demand spatial scales (Census Tracts, ZCTAs/ZIP, MSAs), curated Census endpoints (Data Profiles DP02–DP05), longitudinal price harmonization (CPI-U inflation deflators), Census sentinel code sanitization, and high-performance panel data outputs (Tidy Panel, Codebooks, Parquet).

---

## Architectural Pillars & Detailed Specifications

```text
                                CENSUS RESEARCH ENGINE
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                  │
 │  1. STATISTICAL RIGOR          2. EXTENDED GEOGRAPHIES      3. PROFILE ENDPOINTS │
 │  • MOE & SE Derivation         • Census Tracts (11-digit)   • DP02 Social        │
 │  • CV Reliability Flags        • ZCTAs / ZIP Codes          • DP03 Economic      │
 │  • Z-Test Significance        • MSAs / CBSAs               • DP04 Housing       │
 │  • Sentinel (-666M) Filtering  • National Benchmark (US:1)  • DP05 Demographics  │
 │                                                                                  │
 │  4. TIME-SERIES HARMONIZATION  5. DATA ENGINEERING          6. INTERFACES        │
 │  • CPI-U Inflation Deflator    • Tidy Panel (GEOID, Year)   • Desktop GUI Pass   │
 │  • 2010-2020 Boundary Warning  • Automated Codebooks        • Claude MCP Server  │
 │  • SQLite Query Cache          • Parquet Columnar Export    • Headless Python SDK│
 │                                                                                  │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

---

### Pillar 1: Statistical Rigor & Demographic Methodology

ACS 5-year data represents sample estimates subject to sampling variability. Research standards (OMB, Census Bureau, academic journals) require reporting and analyzing this uncertainty.

#### 1.1 Pairwise Estimate & MOE Pairing
- The Census API returns estimates (`E`) and Margins of Error (`M`) at the 90% confidence level.
- `census.export` will systematically pair estimates with their corresponding MOE rather than treating them as disconnected variables.
- Structure: For each variable code `B01001_001`, maintain both `B01001_001E` (Estimate) and `B01001_001M` (Margin of Error).

#### 1.2 Automated Derivation of Standard Errors (SE) & Coefficients of Variation (CV)
- **Standard Error Calculation**:
  $$SE = \frac{MOE_{90}}{1.645}$$
- **Coefficient of Variation (CV)**:
  $$CV = \left(\frac{SE}{\text{Estimate}}\right) \times 100\%$$
- **Reliability Categorization (Census Standard)**:
  - $CV \le 15\%$: **High Reliability** (Green flag, standard reporting).
  - $15\% < CV \le 30\%$: **Moderate Reliability** (Yellow flag, interpret with caution).
  - $CV > 30\%$: **Low Reliability** (Red flag, extreme sampling error, avoid standalone policy conclusions).

#### 1.3 Statistical Significance Hypothesis Testing ($Z$-Test)
- When comparing two estimates $\hat{X}_1$ and $\hat{X}_2$ (e.g., Austin median income in 2015 vs. 2022, or County A vs. County B):
  $$Z = \frac{|\hat{X}_1 - \hat{X}_2|}{\sqrt{SE_1^2 + SE_2^2}}$$
- Provide two-tailed significance determination:
  - $|Z| \ge 1.645$: Statistically significant at $\alpha = 0.10$ (90% confidence).
  - $|Z| \ge 1.960$: Statistically significant at $\alpha = 0.05$ (95% confidence).
  - $|Z| \ge 2.576$: Statistically significant at $\alpha = 0.01$ (99% confidence).

#### 1.4 Additive Margin of Error Propagation
- When researchers aggregate categories (e.g. summing age brackets $18\text{--}24$ and $25\text{--}34$):
  $$\text{Combined Estimate} = \sum_{i=1}^k \hat{X}_i$$
  $$\text{Combined MOE} = \sqrt{\sum_{i=1}^k MOE_i^2}$$
- Add an aggregation helper function in `census.export` ensuring proper quadrature error propagation.

#### 1.5 Census Sentinel Value & Missing Code Filtering
The Census Bureau encodes missing, top-coded, or non-applicable values as large negative integers:
- `-666666666`: Estimate or margin of error not applicable or cannot be calculated.
- `-888888888`: Not applicable or estimate missing.
- `-999999999`: Top-coded (e.g., Median Income `$250,000+`) or bottom-coded interval.
- `-555555555`: Annotation flag.
- `-222222222`, `-333333333`: Controlled or missing margin of error.

**Requirement**: `census.export.clean_census_sentinels(df)` will replace these sentinels with `NaN` (or categorical indicators like `>250,000`) so researchers do not accidentally calculate negative median incomes or negative ages.

---

### Pillar 2: Extended Research Geographies & Spatial Identifiers

Researchers need spatial granularity beyond cities and whole states.

#### 2.1 Census Tracts (Summary Level 140)
- **Importance**: The universal geographic unit for spatial econometrics, neighborhood studies, redlining research, and health disparities.
- **API Pattern**:
  `for=tract:*&in=state:{state_code}&in=county:{county_code}`
- **Coverage**: All ~85,000 tracts across all 3,143 US counties.

#### 2.2 ZIP Code Tabulation Areas / ZCTAs (Summary Level 860)
- **Importance**: Necessary for merging Census demographics with external datasets indexed only by 5-digit ZIP codes (e.g., clinical health registries, voter files, business licenses).
- **API Pattern**:
  `for=zip code tabulation area:*` or `for=zip code tabulation area:{zip_code}`
- **Integration**: Leverage the existing workspace [`zip2fips.json`](file:///c:/Users/hobo/Desktop/US-Census-5-Year-ACS-Data-Batch-Downloader/zip2fips.json) to allow users to input a ZIP code or County and resolve corresponding boundaries.

#### 2.3 Metropolitan Statistical Areas / CBSAs (Summary Level 310)
- **Importance**: Standard boundary for regional labor market, wage, and housing cost studies.
- **API Pattern**:
  `for=metropolitan statistical area/micropolitan statistical area:*`

#### 2.4 National US Benchmark (Summary Level 010)
- **Importance**: Provides the reference baseline against which state, county, or place values are indexed.
- **API Pattern**:
  `for=us:1`

#### 2.5 Standard 11-Digit Composite `GEOID` Construction
In raw API responses, `state`, `county`, `tract`, and `place` are returned as disparate columns. Researchers require the standard Census `GEOID` string to join with TIGER/Line GIS shapefiles in GeoPandas, QGIS, or ArcGIS:
- **Tract `GEOID` (11 digits)**: `{state:02d}{county:03d}{tract:06d}`
- **County `GEOID` (5 digits)**: `{state:02d}{county:03d}`
- **Place `GEOID` (7 digits)**: `{state:02d}{place:05d}`
- **ZCTA `GEOID` (5 digits)**: `{zcta:05d}`

---

### Pillar 3: Specialized Endpoints (Data Profiles & Subject Tables)

Detailed tables (`/acs/acs5`) only provide raw counts; researchers must manually locate denominators and compute percentages. The Census Bureau provides dedicated pre-calculated endpoints:

#### 3.1 Data Profile Endpoint (`/acs/acs5/profile`)
Provides pre-computed rates, percentages, and median estimates with built-in denominators:
- `DP02`: **Selected Social Characteristics** (Educational attainment %, veteran status, foreign-born %, disability rate, language spoken at home).
- `DP03`: **Selected Economic Characteristics** (Unemployment rate, poverty rate %, median household income, industry shares, health insurance coverage %).
- `DP04`: **Selected Housing Characteristics** (Homeownership rate %, gross rent as % of income, housing units, vacant units).
- `DP05`: **Demographic and Housing Estimates** (Age pyramid, sex ratio, race/ethnicity percentages).

#### 3.2 Subject Tables Endpoint (`/acs/acs5/subject`)
Pre-aggregated research tables on specific domains:
- `S1501`: Educational Attainment
- `S1701`: Poverty Status in the Past 12 Months
- `S1901`: Income in the Past 12 Months
- `S2701`: Health Insurance Coverage Status

#### 3.3 Comparison Profiles (`/acs/acs5/cprofile`)
Includes built-in statistical significance flags (`*` indicator) comparing 5-year periods directly from the Census Bureau.

---

### Pillar 4: Time-Series & Longitudinal Harmonization

#### 4.1 CPI-U Inflation Deflator Engine
- **Problem**: In ACS 5-year datasets, financial figures (median income `B19013`, rent `B25064`, home value `B25077`) are reported in dollars adjusted to the final year of that specific 5-year period (e.g. 2012 ACS is in 2012 dollars; 2022 ACS is in 2022 dollars).
- **Solution**: Embed a Bureau of Labor Statistics (BLS) **CPI-U** (Consumer Price Index for All Urban Consumers) annual deflator lookup table:
  $$\text{Real Dollars}_{\text{base}} = \text{Nominal Dollars}_t \times \left(\frac{\text{CPI}_{\text{base}}}{\text{CPI}_t}\right)$$
- Allow researchers to select a target base year (e.g., "Adjust all financial values to 2022 constant dollars").

#### 4.2 2010 vs. 2020 Decennial Census Boundary Shift Advisory
- **Problem**: In 2020, Census tract and county subdivision boundaries underwent decennial redistricting. Many tracts were split, merged, or renumbered between the 2019 ACS and 2020 ACS.
- **Solution**: Add an automated advisory in logs and metadata when a multi-year query spans across the 2019/2020 divide, warning researchers that geographic boundaries may not be strictly contiguous.

#### 4.3 SQLite Local Query Cache
- Store API responses keyed by `(year, state, geo_level, entity_code, table_code)` in a local SQLite database (`.cache/census_cache.db`).
- Re-running analysis scripts or refining panel models avoids re-requesting identical data from the Census API, eliminating rate limit risks and enabling offline reproducibility.

---

### Pillar 5: Research Toolchain Output Formats

#### 5.1 Tidy Longitudinal Panel Format
- Format optimized for panel data regression in R (`fixest`, `plm`), Stata, and Python (`statsmodels`, `linearmodels`):
  - Primary Index: `(GEOID, Year)`
  - Columns: Sanitized short slugs (e.g., `pop_total`, `med_hh_income_est`, `med_hh_income_moe`, `poverty_rate_pct`).

#### 5.2 Automated Codebook / Data Dictionary Generation (`codebook.csv`)
For every batch extraction, automatically output a companion codebook file:
- `Variable_Code`: Raw Census code (e.g., `B19013_001E`).
- `Variable_Slug`: Sanitized identifier (e.g., `median_household_income`).
- `Measurement_Type`: `Estimate`, `Margin of Error`, `Percentage`, or `Annotation`.
- `Full_Description`: Human-readable label from `variables.json`.
- `Universe`: Target universe (e.g., `Households`, `Total Population`).
- `Unit`: Dollars, Count, Percent, or Ratio.

#### 5.3 Apache Parquet & Arrow Export
- For high-volume queries (e.g., all 85,000 tracts across 10 years):
  - CSV files can reach 10+ GB and consume excessive memory.
  - Export to `.parquet` format with snappy compression: reduces file size by 85–92%, retains native column datatypes (int64, float64, string), and integrates directly with DuckDB, Polars, and Pandas.

---

### Pillar 6: Interface Upgrades (GUI, CLI & Claude MCP Tools)

#### 6.1 Claude Code & Claude Desktop MCP Tools Expansion
Expose the new research capabilities directly to Claude via `mcp_server.py`:
- `list_tracts(state_code, county_code)`: Lists all Census tracts in a county.
- `list_zctas(state_code)`: Lists ZIP Code Tabulation Areas in a state or resolves ZIP codes using `zip2fips.json`.
- `calculate_significance(est1, moe1, est2, moe2, confidence_level)`: Runs two-tailed $Z$-test and returns $Z$-statistic, $p$-value, and significance verdict.
- `calculate_cv(estimate, moe)`: Calculates standard error, CV %, and reliability flag.
- `query_data_profile(profile_code, state_code, geo_level, ...)`: Direct query for `DP02`, `DP03`, `DP04`, `DP05`.
- `adjust_inflation(value, from_year, to_year)`: Returns CPI-U constant dollar adjusted value.
- `generate_codebook(table_code)`: Emits machine-readable data dictionary.

#### 6.2 Desktop GUI Enhancements ([acs_downloader.py](file:///c:/Users/hobo/Desktop/US-Census-5-Year-ACS-Data-Batch-Downloader/acs_downloader.py))
- Geography selector: Add `Tracts in County` and `ZCTA (ZIP)`.
- Format selector: Add `Tidy Panel CSV`, `Parquet (.parquet)`, and `Include Codebook`.
- Statistical toggle: Checkbox to `Calculate Standard Errors & CVs`.
- Inflation toggle: Checkbox to `Adjust Financial Values to Constant Dollars (CPI-U)`.

---

## Implementation Roadmap

```text
PHASE 5a: Statistical Engine & Data Profiles
├── Pair E & M variables, calculate SE, CV, and Z-test statistics
├── Implement sentinel cleaner (-666666666 -> NaN)
└── Implement Data Profile endpoints (DP02, DP03, DP04, DP05)

PHASE 5b: Extended Geographies & Composite GEOIDs
├── Implement Census Tract query handler (Summary Level 140)
├── Implement ZCTA query handler (Summary Level 860) with zip2fips.json
└── Generate standard 11-digit / 5-digit composite GEOID columns

PHASE 5c: Longitudinal Harmonization & Output Formats
├── Integrate BLS CPI-U annual deflator table & constant dollar converter
├── Add 2019-2020 decennial boundary shift advisory flag
├── Implement Tidy Panel data generator & codebook.csv exporter
└── Implement Apache Parquet export (.parquet)

PHASE 5d: Claude MCP Server & Desktop GUI Expansion
├── Register new tools in mcp_server.py for Claude Code & Claude Desktop
├── Update acs_downloader.py GUI with new controls and toggles
└── Add comprehensive unit test suite in tests/
```

---

## Testing & Validation Strategy

1. **Statistical Verification**:
   - Verify SE formula against Census Bureau published tables.
   - Verify $Z$-test p-values against standard normal distribution values ($Z = 1.96 \implies p = 0.05$).
2. **Geography Validation**:
   - Validate 11-digit tract GEOID generation for known counties (e.g. New York County 36061, Travis County TX 48453).
   - Validate ZCTA queries using `zip2fips.json`.
3. **Data Profile Tests**:
   - Verify `DP03` economic queries return percentages and medians correctly.
4. **Inflation Verification**:
   - Verify 2010 nominal dollar conversion to 2022 constant dollars against official BLS CPI-U series.
5. **Format Validation**:
   - Validate Parquet schemas, tidy panel index uniqueness `(GEOID, Year)`, and codebook completeness.

---

## Ready to Proceed

This plan covers all five core pillars and tangential gaps. When you are ready, we will branch `phase5/research-grade-pipeline` and begin implementing **Phase 5a** (Statistical Engine & Data Profiles).
