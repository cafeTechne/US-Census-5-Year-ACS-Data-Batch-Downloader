# Case study: price-adjusted county median household income, Michigan and New York (live ACS data)

This is the project's worked research workflow on **live** American Community Survey data
(PLAN_ROUND_13 E1, owner decision D13-2). Every step is a `python -m census` command; there
are no Python scripts. Michigan (state 26) and New York (state 36) are only arguments: the
same commands work for any state.

Unlike [the reference workflow](reference_workflow.md), which runs offline on synthetic
frozen inputs, this one downloads real data from the Census Data API, the Census variance
replicate (VRE) files and Census cartographic boundary files. Results are real ACS
estimates and can change if the Census Bureau revises the release.

The study runs two ways (PLAN_ROUND_14 A4):

1. **Confirmatory** (section 1): one pre-registered manifest,
   [`examples/manifests/mi_ny_income_education.json`](../examples/manifests/mi_ny_income_education.json),
   run with `run_manifest`. It fixes the states, variables, derivation, price adjustment,
   model, hypothesis and correction before any data are pulled. This is the result to report.
2. **Exploratory, as sensitivity analysis** (sections 2-7): the same analysis built step by
   step (Round 13), plus what the manifest does not cover: maps, a nominal-dollar model and
   replicate SEs for the covariate.

## Purpose and research question

How does county median household income compare across Michigan and New York counties once
dollars are expressed in national-average purchasing power with BEA Regional Price Parities
(RPPs)? How is that price-adjusted income associated with the share of adults holding a
bachelor's degree?

| Item | Choice |
| :--- | :--- |
| Release | ACS 5-year 2020-2024 (release 2024), the latest available (`python -m census vintages`) |
| Units | All counties: 83 in Michigan, 62 in New York (145) |
| Outcome | Median household income, `B19013_001` (2024 inflation-adjusted dollars), with its published 90% MOE |
| Price adjustment | Divide by the BEA 2024 RPP (U.S. = 100) of the county's area: its metropolitan statistical area (MSA) when the county is in one, else the state's nonmetropolitan portion |
| Covariate | Percent of adults 25+ with a bachelor's degree or higher, from `B15002` (sex by educational attainment): (`_015`..`_018` + `_032`..`_035`) / `_001` x 100 |
| Model | OLS `median_hh_income ~ pct_bachelors + state`, replicate refits on the 80 VRE replicates, 90% intervals, `population` target |

**Why this covariate.** Educational attainment is the standard area-level correlate of
income (human capital). `B15002` is a detailed table on the Census VRE list for county
files, so the covariate's own sampling error enters the replicate refits; `S1501` or
`DP02` give the same percentage but have no published replicates. A state indicator absorbs
any level difference between the two states that the price adjustment does not remove.

## Setup (once, not executed by the tests)

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
. .venv/bin/activate   # every `python` below is this venv's interpreter
```

Get your own Census API key (https://api.census.gov/data/key_signup.html) and set
`CENSUS_API_KEY` in the environment or in a `.env` file at the repository root
(`census/api.py` loads it with `python-dotenv`). Never print or commit the key. The
toolkit's normal query cache (`.cache/`) is used; nothing here marks it synthetic.

The whole workflow takes about 2 minutes on a cold cache and downloads about 30 MB (query
cache, the B15002 county VRE file and the 2024 boundary files) into `.cache/`; outputs take
about 8 MB.

## 0. Workspace

Run the commands from the repository root. All outputs go under `case_study_output/`
(gitignored; never committed); paths are written out in full so every command can be pasted
as is into a POSIX shell (bash, zsh).

## 1. Confirmatory run: the pre-registered manifest

```bash
python -m census tool create_manifest --arg manifest_path=examples/manifests/mi_ny_income_education.json --json
python -m census tool run_manifest --arg manifest_path=examples/manifests/mi_ny_income_education.json --arg output_dir=case_study_output/confirmatory --json
```

`create_manifest` validates the manifest offline. Expected: `"valid": true` and
`"hash": "d8905f57625e1d1862a63ca0f6deeb364a7643385fc4a700b6691e855e3bfcb0"`. A different
hash means a different pre-registration. The MCP tool `run_manifest` takes the same
arguments and gives the same result.

What the manifest fixes:

| Manifest entry | Value |
| :--- | :--- |
| `geography` | `{"level": "county", "states": ["26", "36"]}`: each table is extracted per state and the two states are stacked (145 counties) |
| `variables` | `median_hh_income` = `2024:B19013_001`; `ed_total` = `2024:B15002_001`, and the eight bachelor's-or-higher cells `B15002_015`..`_018` and `_032`..`_035` |
| `derivations` | `pct_bachelors`: proportion of the eight cells over `ed_total`, as a percent |
| `adjustments` | `{"type": "rpp", "variables": ["median_hh_income"], "rpp_year": 2024, "areas": "county_metro"}` |
| `exclusions` | CV screen at 30% on `median_hh_income` and `pct_bachelors`, flag only |
| `analyses` | m1: `fit_replicate`, OLS `median_hh_income ~ pct_bachelors + C(GEOID.str[:2])`, `population` target, 90% intervals |
| `hypotheses` | H1: coefficient of `pct_bachelors` > 0 (one-sided); Holm correction (one hypothesis) |

The `adjustments` step uses the same code as the `place_adjust_frame` tool (section 3):
each county gets its MSA's 2024 RPP, or the state's nonmetropolitan-portion RPP outside a
metropolitan CBSA, and the estimate and SE are divided by RPP/100 (replicates too, when
replicates are supplied). Multi-state manifests get no automatic state effect: the formula
names the state term `C(GEOID.str[:2])` explicitly. Without `rpp_year` the latest year in
the RPP table would be used and recorded; a year other than the release end year is
recorded as a note, not a deviation.

Expected in the JSON output: `"analysis_status": "confirmatory"`, `"deviations": []`,
`data_source` `{"synthetic": false, ...}`, and for H1:

| Term | Estimate | se_model | 90% CI (from se_model) | One-sided p (Holm) |
| :--- | ---: | ---: | :--- | ---: |
| pct_bachelors | 811.00 | 77.03 | [684.29, 937.71] | 3.2e-26 |

`confirmatory/bundle/results/m1.json` also gives New York vs Michigan 1,643.83 ([-965.49,
4,253.15]) and the intercept 51,698.07. `confirmatory/bundle/results/run.json` records
`n_units` 145, no county flagged by the CV screen, and under `adjustments`: the RPP year
(2024, `rpp_year_source` `manifest`), the 31 RPP areas used (29 MSAs and the two
nonmetropolitan portions) with their RPP values and county counts, the CBSA boundary file,
and the SHA-256 of the bundled RPP table
(`3e726a9d274636a2cce1301bac17000b66c71cb37090b9ac001c5c1082fd2dd9`; a table from
`CENSUS_PRICE_PARITY_DIR` would be recorded with its path and hash as `custom`).
`bundle.json` carries the same RPP provenance and `analysis_status: confirmatory`, and
`methods.md` describes the adjustment.

**Comparison with the exploratory result (section 4).** The confirmatory coefficient,
`se_model` and interval are identical to the Round 13 exploratory result (811.00, 90% CI
[684.29, 937.71]), and so are the state and intercept terms. They have to be: both routes use
the same API responses, the same county-to-RPP assignment, the same derived share and the
same OLS fit, and with the `population` target the interval uses the model SE. There is one
difference. The confirmatory run has **no `se_sampling`**. No replicates are supplied, and
they could not be complete: `B19013` has no Census replicates, and a confirmatory run uses
`replicate_policy="strict"`, so supplying only the `B15002` replicates would stop m1 with a
blocking `replicates_incomplete` deviation. The covariate-only `se_sampling` (11.98) is
reported by the exploratory route below as a sensitivity result. It does not enter the
stated interval in either route. In the manifest frame `pct_bachelors_se` is the ACS
approximation rather than the exact replicate SE; this affects only the CV screen, which
flags no county in either route.

**What "confirmatory" means here.** The manifest was written after the Round 13
exploratory analysis of the same data, so this run confirms that the analysis reproduces
exactly under a fixed protocol. It is not an independent test of the hypothesis. An
independent confirmation would apply the same manifest, with only `states` or `releases`
changed, to data not yet analysed, for example other states or the next ACS release.

The offline test `tests/test_manifest_multistate.py` runs these two commands on frozen
copies of the four API responses (trimmed to the manifest's variables) with network access
refused, and checks the numbers above for the CLI and MCP routes. The opt-in live test
(`CENSUS_LIVE_TESTS=1`) runs them against the Census API and the boundary files.

## Sensitivity analysis: the exploratory route

Sections 2-7 build the same model step by step with separate tools. Results from this
route are labelled `exploratory`.

## 2. Extraction with MOEs and provenance

```bash
python -m census extract --table B19013 --geo county --state 26 --years 2024 --variables B19013_001 --reliability --codebook --slug-style code --out "case_study_output/extract/26/B19013" --json
python -m census extract --table B19013 --geo county --state 36 --years 2024 --variables B19013_001 --reliability --codebook --slug-style code --out "case_study_output/extract/36/B19013" --json
python -m census extract --table B15002 --geo county --state 26 --years 2024 --variables B15002_001,B15002_015,B15002_016,B15002_017,B15002_018,B15002_032,B15002_033,B15002_034,B15002_035 --reliability --codebook --slug-style code --out "case_study_output/extract/26/B15002" --json
python -m census extract --table B15002 --geo county --state 36 --years 2024 --variables B15002_001,B15002_015,B15002_016,B15002_017,B15002_018,B15002_032,B15002_033,B15002_034,B15002_035 --reliability --codebook --slug-style code --out "case_study_output/extract/36/B15002" --json
```

Expected for each: `"success": true`, one call, `"failed_calls": []`, and every concept
with status `stable` in `*_concordance_issues.json` (`status_counts`). The progress line
`concordance policy: fail` on stderr is the policy (stop on blocking issues), not a result. Each folder holds the panel (`*_county_panel_2024.csv`: estimate, MOE, SE =
MOE / 1.645, CV, reliability), the codebook, and `*_extraction_metadata.json` (endpoint
`https://api.census.gov/data/{year}/acs/acs5`, variables, concept IDs, metadata vintage
hashes, package version and git commit), which `python -m census replay` can re-run (step 7).

## 3. Price adjustment and maps, one state at a time

```bash
python -m census tool build_analysis_frame --arg input_path=case_study_output/extract/26/B19013/B19013_county_panel_2024.csv --arg 'variables={"median_hh_income": "B19013_001"}' --arg year=2024 --arg output_path=case_study_output/frames/income_26.parquet --json
python -m census tool place_adjust_frame --arg input_path=case_study_output/frames/income_26.parquet --arg 'variables=["median_hh_income"]' --arg year=2024 --arg output_path=case_study_output/frames/income_26_rpp.parquet --save case_study_output/results/rpp_26.json --json
python -m census tool make_map --arg input_path=case_study_output/frames/income_26_rpp.parquet --arg column=median_hh_income --arg level=county --arg year=2024 --arg se_col=median_hh_income_se --arg "label=Median household income (RPP-adjusted 2024 $)" --arg "data_source=ACS 5-year 2020-2024, table B19013, divided by BEA 2024 Regional Price Parities" --arg output_dir=case_study_output/maps/26 --save case_study_output/results/map_26.json --json
python -m census tool make_map --arg input_path=case_study_output/frames/income_26_rpp.parquet --arg column=median_hh_income --arg level=county --arg year=2024 --arg kind=uncertainty --arg se_col=median_hh_income_se --arg "label=median household income" --arg "data_source=ACS 5-year 2020-2024, table B19013" --arg output_dir=case_study_output/maps/26 --save case_study_output/results/cv_map_26.json --json
python -m census tool make_table --arg kind=descriptive --arg input_path=case_study_output/frames/income_26_rpp.parquet --arg 'variables=["median_hh_income"]' --arg output_dir=case_study_output/tables --arg name=income_26 --json
python -m census tool build_analysis_frame --arg input_path=case_study_output/extract/36/B19013/B19013_county_panel_2024.csv --arg 'variables={"median_hh_income": "B19013_001"}' --arg year=2024 --arg output_path=case_study_output/frames/income_36.parquet --json
python -m census tool place_adjust_frame --arg input_path=case_study_output/frames/income_36.parquet --arg 'variables=["median_hh_income"]' --arg year=2024 --arg output_path=case_study_output/frames/income_36_rpp.parquet --save case_study_output/results/rpp_36.json --json
python -m census tool make_map --arg input_path=case_study_output/frames/income_36_rpp.parquet --arg column=median_hh_income --arg level=county --arg year=2024 --arg se_col=median_hh_income_se --arg "label=Median household income (RPP-adjusted 2024 $)" --arg "data_source=ACS 5-year 2020-2024, table B19013, divided by BEA 2024 Regional Price Parities" --arg output_dir=case_study_output/maps/36 --save case_study_output/results/map_36.json --json
python -m census tool make_map --arg input_path=case_study_output/frames/income_36_rpp.parquet --arg column=median_hh_income --arg level=county --arg year=2024 --arg kind=uncertainty --arg se_col=median_hh_income_se --arg "label=median household income" --arg "data_source=ACS 5-year 2020-2024, table B19013" --arg output_dir=case_study_output/maps/36 --save case_study_output/results/cv_map_36.json --json
python -m census tool make_table --arg kind=descriptive --arg input_path=case_study_output/frames/income_36_rpp.parquet --arg 'variables=["median_hh_income"]' --arg output_dir=case_study_output/tables --arg name=income_36 --json
```

`place_adjust_frame` finds each county's core-based statistical area (CBSA) by overlaying
the Census 2024 county and CBSA cartographic boundary files (the CBSA file carries OMB
bulletin 23-01, the delineation BEA's MSA RPPs use; every county matched one CBSA with
100% of its area or none). Counties in a metropolitan CBSA get that MSA's RPP; counties in
a micropolitan CBSA or outside any CBSA get the state's nonmetropolitan-portion RPP. The
estimate and its SE are divided by RPP/100 (the CV is unchanged). `rpp_26.json` and
`rpp_36.json` list every RPP area used, with its value and county count.

Expected RPP areas (2024 RPPs, U.S. = 100):

| State | Areas | Nonmetropolitan portion | Selected metropolitan areas |
| :--- | :--- | :--- | :--- |
| Michigan | 17: 16 MSAs (30 counties) and the nonmetro portion | 88.972 (53 counties) | Detroit-Warren-Dearborn 100.298 (6 counties), Grand Rapids-Wyoming-Kentwood 95.546 (5), Ann Arbor 100.880 (1) |
| New York | 14: 13 MSAs (37 counties) and the nonmetro portion | 91.613 (25 counties) | New York-Newark-Jersey City NY-NJ 112.563 (10 counties), Albany-Schenectady-Troy 99.566 (5), Rochester 97.035 (5) |

Expected descriptive statistics of RPP-adjusted county median household income
(`tables/income_26.md`, `tables/income_36.md`; median SE is the median of the counties'
sampling SEs):

| State | Counties | Median | Min | Max | Median SE | Median CV | CV > 30% (share of counties) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Michigan | 83 | 70,829 | 55,505 | 106,479 | 1,676 | 2.4% | 0% |
| New York | 62 | 76,172 | 43,243 | 129,885 | 1,718 | 2.2% | 0% |

### Maps

Each state has two journal-format maps (PDF, SVG, EPS, PNG at 300 dpi, a values CSV with
estimate, SE, 90% MOE, interval bounds and CV per county, a caption and a provenance JSON):

- `maps/<state>/choropleth.*`: RPP-adjusted median household income in five quantile
  classes. The **uncertainty layer** is built in: the legend reports *class separability*,
  the share of adjacent-class county pairs whose 90% confidence intervals do not overlap
  (Sun and Wong 2010), and counties with CV above 30% are hatched. Expected: separability
  52% (Michigan) and 42% (New York), no hatched county. So county estimates are individually
  precise (CVs of 1-7%), yet 48% (Michigan) and 58% (New York) of neighbouring-class county
  pairs cannot be told apart at 90% confidence: read class boundaries as approximate, and compare two counties
  with a significance test, not by their colours.
- `maps/<state>/uncertainty.*`: the coefficient of variation of each county's estimate
  (classes at 12% and 30%).

Small copies of the two choropleth PNGs are kept in the repository:
[`docs/case_study/choropleth_26.png`](case_study/choropleth_26.png) and
[`docs/case_study/choropleth_36.png`](case_study/choropleth_36.png).

## 4. Regression with replicate standard errors

```bash
python -m census tool build_analysis_frame --arg 'input_path=["case_study_output/extract/26/B19013/B19013_county_panel_2024.csv", "case_study_output/extract/36/B19013/B19013_county_panel_2024.csv", "case_study_output/extract/26/B15002/B15002_county_panel_2024.csv", "case_study_output/extract/36/B15002/B15002_county_panel_2024.csv"]' --arg 'variables={"median_hh_income": "B19013_001"}' --arg 'derive=[{"name": "pct_bachelors", "op": "proportion", "num": ["B15002_015E", "B15002_016E", "B15002_017E", "B15002_018E", "B15002_032E", "B15002_033E", "B15002_034E", "B15002_035E"], "den": "B15002_001E", "as_percent": true}]' --arg use_replicates=true --arg table_code=B15002 --arg year=2024 --arg output_path=case_study_output/frames/model_frame.parquet --json
python -m census tool place_adjust_frame --arg input_path=case_study_output/frames/model_frame.parquet --arg 'variables=["median_hh_income"]' --arg year=2024 --arg replicates_path=case_study_output/frames/model_frame_replicates.parquet --arg output_path=case_study_output/frames/model_frame_rpp.parquet --arg replicates_output_path=case_study_output/frames/model_frame_replicates_rpp.parquet --save case_study_output/results/rpp_model.json --json
python -m census tool fit_model --arg input_path=case_study_output/frames/model_frame_rpp.parquet --arg "formula=median_hh_income ~ pct_bachelors + C(GEOID.str[:2])" --arg replicates_path=case_study_output/frames/model_frame_replicates_rpp.parquet --arg target=population --arg figure_dir=case_study_output/figures --save case_study_output/results/regression.json --json
python -m census tool fit_model --arg input_path=case_study_output/frames/model_frame.parquet --arg "formula=median_hh_income ~ pct_bachelors + C(GEOID.str[:2])" --arg replicates_path=case_study_output/frames/model_frame_replicates.parquet --arg target=population --save case_study_output/results/regression_nominal.json --json
```

The first command joins the four extractions into one row per county (the two tables are
joined on GEOID; the two states are stacked; a cell given different values by two files
would be an error), derives `pct_bachelors` (`derive` names use the API variable codes such as `B15002_015E`;
the panel CSV stores them as `b15002_015_est`, and the tool maps between the two) and downloads the 2024 county VRE file for
`B15002`, evaluating the same derivation on each of the 80 replicates (so `pct_bachelors_se`
is the exact replicate SE). `C(GEOID.str[:2])` is the state indicator (New York vs Michigan).
The last command is a sensitivity run on nominal (unadjusted) dollars. Each `fit_model` call takes a few seconds (80 refits).

**Replicates for the outcome do not exist.** `B19013` is not on the Census 2020-2024 VRE
table list (`python -m census replicates --table B19013 --year 2024 --geo county` says so),
so the outcome has its published MOE only. `fit_model` therefore reports
`REPLICATES INCOMPLETE` (`median_hh_income` is held at its estimate in the refits):
`se_sampling` measures the coefficient's variability from the covariate's sampling error
alone and understates total sampling variability. This does not affect the stated interval
(next section), which uses the model SE. (Medians from the distribution table `B19001`,
which has replicates, would be a different estimate from the published median.)

### Result

Expected (N = 145 counties; no county above the 30% CV screen), from
`results/regression.json`:

| Term | Estimate | se_model | se_sampling (covariate only) | 90% CI (from se_model) |
| :--- | ---: | ---: | ---: | :--- |
| pct_bachelors | 811.00 | 77.03 | 11.98 | [684.29, 937.71] |
| New York (vs Michigan) | 1,643.83 | 1,586.35 | 155.06 | [-965.49, 4,253.15] |
| Intercept | 51,698.07 | 2,189.56 | 271.95 | [48,096.56, 55,299.58] |

Units: RPP-adjusted 2024 dollars of county median household income per percentage point of
adults 25+ with a bachelor's degree or higher. Every one of the 80 replicate refits gives a
positive `pct_bachelors` coefficient (`diagnostics.replicate_stability`).

Sensitivity (`results/regression_nominal.json`, nominal dollars): `pct_bachelors` 1,057.96
(90% CI [906.06, 1,209.87]); New York vs Michigan 4,728.55 ([1,600.35, 7,856.75]). Price
adjustment lowers the slope by about a quarter, because counties with more college graduates
are concentrated in higher-price metropolitan areas, and it removes the New York premium at
a given education level (its interval then includes 0).

### What the interval means

The interval target is stated by `census.results.interval_description` (stored as
`interval` in `results/regression.json`):

> 90% normal intervals and p-values from se_model (includes model), population target;
> estimand: the population (superpopulation) association of median_hh_income with the
> predictors among areas (area level, not individuals); an association, not a causal effect;
> REPLICATES INCOMPLETE: sampling SE may be understated (stochastic variable(s) without
> replicates: ['median_hh_income'])

So [684, 938] is a 90% interval for the general county-level association between education
and price-adjusted income in the superpopulation these counties represent, based on the model
SE, in which the outcome's sampling error is absorbed by the residual. It is not an interval
for "these 145 counties in 2020-2024" (that would be `target=these_areas`, which uses
`se_sampling` and here would be too narrow because the outcome has no replicates). It is an
association between areas, not a statement about individuals and not a causal effect of
education on income. Spatial correlation among counties is not modelled, so the model SE may
also be too small (see Limitations).

## 5. Results bundle and run provenance

```bash
python -m census tool make_results_bundle --arg output_dir=case_study_output/bundle --arg 'results={"regression": "case_study_output/results/regression.json", "regression_nominal": "case_study_output/results/regression_nominal.json", "rpp_model": "case_study_output/results/rpp_model.json", "rpp_26": "case_study_output/results/rpp_26.json", "rpp_36": "case_study_output/results/rpp_36.json", "map_26": "case_study_output/results/map_26.json", "map_36": "case_study_output/results/map_36.json", "cv_map_26": "case_study_output/results/cv_map_26.json", "cv_map_36": "case_study_output/results/cv_map_36.json"}' --arg 'figure_paths=["case_study_output/maps/26/choropleth.pdf", "case_study_output/maps/26/choropleth.png", "case_study_output/maps/26/uncertainty.pdf", "case_study_output/maps/36/choropleth.pdf", "case_study_output/maps/36/choropleth.png", "case_study_output/maps/36/uncertainty.pdf", "case_study_output/figures/coefficients.pdf"]' --arg 'inputs=["case_study_output/extract/26/B19013/B19013_extraction_metadata.json", "case_study_output/extract/36/B19013/B19013_extraction_metadata.json", "case_study_output/extract/26/B15002/B15002_extraction_metadata.json", "case_study_output/extract/36/B15002/B15002_extraction_metadata.json", "case_study_output/frames/model_frame_rpp.parquet", "case_study_output/frames/model_frame_replicates_rpp.parquet"]' --arg "title=Price-adjusted county median household income, Michigan and New York, ACS 2020-2024" --arg 'notes=["Outcome B19013 has no Census variance replicates; se_sampling covers the covariate only.", "RPPs are BEA estimates with their own error, not included in any SE, MOE or interval here.", "ACS 5-year dollars are 2024 dollars; the 2024 RPP is applied to the whole 2020-2024 period."]' --json
```

Expected: `"success": true` and `data_source` = `{"kind": "census_api", "synthetic": false}`.
Figures with the same file name from different folders are prefixed with the folder name in
`bundle/figures/` (`26_choropleth.pdf`, `36_choropleth.pdf`, ...).
`bundle/bundle.json` is the run manifest of this exploratory analysis: every file's SHA-256,
the citation ("U.S. Census Bureau, American Community Survey 5-Year Estimates, 2020-2024;
tables B15002, B19013. Retrieved via the Census Data API"), and `provenance` with
`analysis_status: exploratory`, `data_source`, the SHA-256 of each input (extraction metadata,
model frame, replicate file; listed as `{path, sha256}` entries with the path relative to the inputs' common folder, e.g.
`extract/26/B19013/B19013_extraction_metadata.json`, so the two states' files stay apart) and, per extraction, the table, release, states, endpoint,
package version, git commit and number of failed calls (0). `bundle/methods.md` repeats each
result's summary, interval statement and notes (including the RPP notes from
`place_adjust_frame`). The data source is not declared and not read from whatever cache exists
when the bundle is made: it is what the inputs recorded when their data were read. Each
extraction metadata file records the source observed at extraction time (`data_source`; a cache
marked synthetic by `cache import` of a synthetic file gives `synthetic`). Inputs that record
no source give `unknown`; synthetic and real inputs together give `mixed` with a `MIXED DATA
SOURCES` warning. A saved result with `"success": false` is refused (a failed step is not a
result) unless `allow_failed_results=true`, which lists it under "Failed steps" in `methods.md`.

This route is **exploratory**: its steps were not pre-registered. Section 1 is the
pre-registered, confirmatory run of the same analysis.

## 6. Spot check against the live API

For five counties per state, the extracted `B19013_001` estimate and MOE were compared with
the values the Census Data API returns directly
(requested outside the toolkit and its cache, e.g.
`curl "https://api.census.gov/data/2024/acs/acs5?get=NAME,B19013_001E,B19013_001M&for=county:003,081,125,161,163&in=state:26&key=$CENSUS_API_KEY"`;
the API may refuse requests without a key). They are **identical** for
all ten (checked 2026-11-08):

| County | GEOID | Estimate | MOE |
| :--- | :--- | ---: | ---: |
| Alger County, Michigan | 26003 | 61,286 | 1,930 |
| Kent County, Michigan | 26081 | 82,631 | 1,165 |
| Oakland County, Michigan | 26125 | 97,760 | 1,268 |
| Washtenaw County, Michigan | 26161 | 89,180 | 2,487 |
| Wayne County, Michigan | 26163 | 60,539 | 602 |
| Erie County, New York | 36029 | 72,839 | 1,077 |
| Hamilton County, New York | 36041 | 68,835 | 5,478 |
| Kings County, New York | 36047 | 80,263 | 946 |
| Monroe County, New York | 36055 | 76,382 | 1,188 |
| New York County, New York | 36061 | 103,931 | 1,490 |

The check is the opt-in live test `tests/test_case_study_live.py`, which runs every command
in this document into a temporary directory and repeats the comparison:

```sh
CENSUS_LIVE_TESTS=1 python -m pytest -q tests/test_case_study_live.py
```

No values were taken from data.census.gov.

## 7. Re-running

```bash
python -m census replay "case_study_output/extract/26/B19013/B19013_extraction_metadata.json" --out "case_study_output/replay_26_B19013" --json
```

Expected: `"verified": true`, every output file `identical`. Replay honours the cache; with
`--no-cache` on the extraction commands the API is asked again, and a Census revision would
then show up as changed cells. Re-running all commands reproduces the regression to the
printed precision as long as the API, the VRE file (cached under `.cache/vre` with its
SHA-256) and the boundary files (`.cache/geo`, SHA-256 sidecars) are unchanged. Volatile
fields: timestamps (`generated_at`), absolute paths, the git commit, and figure bytes.

## Limitations

- **RPP error is not in any uncertainty measure.** RPPs are BEA estimates with their own
  error, which BEA does not publish as standard errors. Adjusted MOEs and SEs scale the ACS
  sampling error only, so intervals for RPP-adjusted values are too narrow by an unknown
  amount.
- **One RPP year for a five-year period.** ACS 5-year dollars are already in 2024 dollars
  (CPI-U-RS adjusted by the Census Bureau); the 2024 RPP is applied to the whole period.
- **RPP areas are coarse.** All counties of an MSA share one RPP, and all nonmetropolitan
  counties of a state share the nonmetropolitan-portion RPP; within-area price differences
  (e.g. Manhattan vs the rest of the New York MSA) are not captured.
- **Outcome replicates.** `B19013` has no published variance replicates, so the replicate SE
  covers the covariate only (see section 4). The model-SE interval is unaffected.
- **Measurement error in the covariate.** Replicate refits capture the coefficient's sampling
  variability; they do not correct attenuation from a noisy covariate. With covariate CVs
  of about 3.5% (median) the bias is likely small; `fit_model method=regression_calibration`
  is the check.
- **Pooling two states.** The state indicator removes a level difference only; the slope is
  assumed common to both states. Counties are not independent draws (spatial correlation is
  not modelled; `spatial_autocorrelation` is the check).
- **Confirmatory, not independent.** The manifest (section 1) was registered after the
  exploratory analysis of the same data. It fixes the protocol but does not replicate the
  finding on new data (see section 1).

## Toolkit gaps found while writing this workflow

Closed in Round 13 (with offline tests):

- `build_analysis_frame` took one input file and one replicate table; it now takes a list of
  files (other tables joined on GEOID/Year, other states stacked) and a list of tables.
- No CLI route applied RPPs to a frame (only `adjust_for_place` for a single value); new tool
  `place_adjust_frame` (county-to-MSA assignment from Census boundaries; replicates scaled too).
- No results bundle outside `run_manifest`; new tool `make_results_bundle` (observed data
  source, input hashes, extraction provenance).
- `python -m census tool ... --save PATH` keeps a tool's JSON result as a file for the bundle.
- `make_map` gained `label` (readable legend and caption name). Map legends were clipped at
  the right edge for some area shapes (New York counties): legend titles are now wrapped and
  axes are shrunk until the legend fits the fixed journal width.

Closed in Round 14 (PLAN_ROUND_14 A1-A4, offline tests in `tests/test_manifest_multistate.py`):

- `run_manifest` took a single state and had no place-adjustment step. Manifests now take
  `geography.states` (extracted per state and stacked) and an `adjustments` step
  (`{"type": "rpp", ...}`) that reuses the `place_adjust_frame` code and records RPP
  provenance. Derivations are evaluated on supplied replicates. Section 1 is the result.

Open (documented, not closed):

- Census publishes no replicates for `B19013` (and other published medians not on the VRE
  list); exact replicate SEs for a model with such an outcome are not available.
