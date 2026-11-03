# Agent Protocol: Extraction, Concordance Review, Derived Statistics, and Validation

This protocol governs how agents (and humans) extract multi-year ACS data, decide whether variables are comparable across releases, and record those decisions. The MCP server returns this document from `get_research_protocol`; its rules are enforced in code, not only described here.

Agents are expected to run every step below, including review. The safeguards come from independence (the drafter never reviews its own decision), evidence the code generates and re-verifies, and primary sources, not from requiring a human.

## 1. Why this exists

ACS variable codes do not have stable meanings across releases. The offline audit (`docs/audits/concordance_audit.md`) found, across 2009–2024:

- lines renumbered under new codes (for example, DP02 "average household size" moved from `DP02_0015` to `DP02_0016` in 2019);
- codes reused for different measures (`DP02_0002` was "family households" in 2018 and "married couple family" in 2019);
- definitions changed under the same code (`B27001_006` went from "6 to 17 years" to "6 to 18 years" in 2017);
- large label restructurings in subject tables (2013, 2017) and data profiles.

A panel keyed by code silently mixes these. The pipeline therefore keys columns by **concept** and refuses to merge anything it cannot verify.

## 2. Standard extraction workflow

1. **Find the table and variables.** `search_tables`, then `list_variables` or `describe_variable(code, year)`. Codes are vintage-specific, so check the label in the years you need.
2. **Name your variables.** Pass `variables=[...]` with the codes your analysis uses. Accepted forms:
   - `CODE` (for example `B19013_001` or `DP02_0016PE`): resolved in the **latest** requested year where the code exists;
   - `YEAR:CODE` (for example `2018:DP02_0002`): the measure that code denoted in that year;
   - a concept ID (`c_…`) from a concordance report.
   Always read the plan's `selection` field to confirm each input resolved to the measure you meant.
3. **Plan.** `plan_extraction(...)` with the same arguments as `batch_download`. It returns API call counts, cached calls, the concordance summary, issues, advisories, and `would_block`. It downloads no data; it makes at most one national-level call per year for the continuity check.
4. **If `would_block` is false,** run `batch_download`.
5. **If `would_block` is true,** read `issues` (blocking ones first) and follow section 3. Do not switch to `concordance_mode="code"` or `"strict"` to get around a block for confirmatory analysis.

Single-year requests never block on concordance, because there is nothing to compare. Collating single-year pulls yourself is allowed, but compare codes and labels across years with `describe_variable` or `check_variable_concordance` before pooling them. That is exactly what the concordance does for you.

## 3. Concordance modes

| Mode | Behavior | Use for |
| :--- | :--- | :--- |
| `fail` (default) | Stops with `CONCORDANCE_REVIEW_REQUIRED` if any blocking issue touches a selected variable | Hypothesis tests, anything published or reused |
| `strict` | Proceeds; unresolved items stay in separate columns; issues are listed in the result | Exploration and whole-table browsing |
| `code` | Keys columns by raw code with no checks; multi-year needs `allow_unverified_concordance=True` | Reproducing legacy outputs only |

`fail` is the default because a silently pooled column changes test results without any visible symptom, while a block only costs a review step.

## 4. Issue kinds

| Kind | Blocking | Meaning | Usual resolution |
| :--- | :--- | :--- | :--- |
| `relabeled` | Yes | Same code, similar but different label across years | Review: `merge` if the wording change is cosmetic, `split` if the measure changed |
| `ambiguous` | Yes | Two codes in one year share a label | Review against the table shell; `split` or `exclude` |
| `universe_changed` | Yes | Table universe text differs across years | Review; usually `split` |
| `continuity_flag` | Yes | National values jump significantly and implausibly between adjacent years | Investigate; `split`, or `merge` with a written `continuity_exception` |
| `continuity_unavailable` | Yes | National data missing, so continuity could not be checked | Retry when the API is reachable |
| `metadata_unavailable` | Yes (multi-year) | A year's variable metadata could not be loaded | Refresh the archive (`scripts/fetch_metadata_archive.py`) |
| `invalid_override` | Yes | A crosswalk row failed validation | Fix or remove the row |
| `redefined` | No | Same code, clearly different measure; already split | None |
| `possible_continuation` | No | One concept ends where a similar one starts; kept separate | Optionally `merge` through review |
| `table_absent` | No | The table is not published in that year | None |

Thresholds for `continuity_flag`: the change is significant at 90% (Z-test with the overlapping-period adjustment), and it exceeds 25% relative change (counts and dollars) or 10 percentage points (percentages).

## 5. Override workflow (draft, then independent review)

Decisions are recorded in `crosswalks/<dataset>/<table>.csv`. Each row maps one (year, code) to a decision.

| Decision | Effect | When |
| :--- | :--- | :--- |
| `merge` | Moves the (year, code) into the target concept, so one continuous column | Same measure, cosmetic relabel or renumbering that automatic matching missed |
| `split` | Confirms the (year, code) is its own concept and resolves the review issue | The measure changed |
| `exclude` | Drops the (year, code) from outputs | The line should not be used at all |
| `rule` | A reviewed regex rewrite of one table's labels at one release boundary; every rewritten line that then matches exactly one later line is merged | Systematic redesigns (for example S1701 in 2017 inserted "population for whom poverty status is determined" into every label). Stored in `crosswalks/<dataset>/<table>_rules.csv`; see section 5a |

**When in doubt, choose `split`.** Keeping two measures apart never corrupts data, while a wrong merge does.

### Step A: Drafter

1. Run `check_variable_concordance(table_code, start_year, end_year, variables=[...])` to get the issues, concept IDs, and evidence.
2. Consult a Census primary source: the release's table and geography changes documentation, table shells, or user notes on `census.gov`.
3. Call `draft_crosswalk_override(...)` with your identity in `drafted_by`. The tool generates the evidence (labels, universes, similarity) from archived metadata, runs the continuity check for merges, saves `crosswalks/evidence/<evidence_id>.json`, and returns draft rows with an empty `reviewed_by`.
4. Hand the draft rows and evidence ID to a different reviewer. Do not review your own draft.

### Step B: Reviewer (agent or human)

1. Use a different identity from the drafter (a different agent session, a different model, or a human).
2. Re-derive the conclusion independently: read the evidence file, open the cited `source_url`, and inspect the labels yourself with `describe_variable`. Do not rely on the drafter's rationale.
3. If you agree, call `record_crosswalk_review(row, reviewed_by=<your identity>)`. If you disagree, do not record the row; draft the opposite decision instead, which then needs its own reviewer.

### Requirements enforced by `validate_crosswalks` and at runtime

All rows:
- `rationale`, `drafted_by`, `reviewed_by`, and `reviewed_on` are present;
- identities look like `human:<name>` or `agent:<model>/<session>`, and `reviewed_by` differs from `drafted_by`;
- the code exists in that year's archived metadata.

`merge` rows additionally:
- `source_url` is on `census.gov`;
- `evidence_id` names a file in `crosswalks/evidence/` whose content hash matches its ID and whose metadata still matches the archive when regenerated, so hand-edited or stale evidence is rejected;
- the evidence's continuity check passed, or `continuity_exception` explains why the discontinuity is real.

Invalid rows are never applied; they surface as `invalid_override` issues.

### Identity convention

- Humans: `human:<name or handle>`
- Agents: `agent:<model-id>/<session-or-run-id>`, for example `agent:claude-opus-5-5/2026-11-01T21-40Z-a7f3`

Use a stable session identifier, so that an audit can confirm the drafter and reviewer were different sessions.

## 5a. Table-scoped rules

`draft_rule_override` / `record_rule_review` (CLI: `draft-rule`, `record-rule`) follow the same two-identity process as single-row overrides. A rule applies `re.sub(pattern, replacement, label)` to the canonical labels of the earlier release of one table, at one adjacent release boundary. Validation rejects a rule that changes any number in a label (age bands and dollar bounds must stay blocked), maps two earlier lines to one later line, creates no new match, overlaps another rule, or cites a source outside `census.gov`. Evidence (every rewrite and match) is generated and re-verified. Merged concepts are still continuity-checked; a rule grants no exemption. Full procedure: `docs/rule_overrides.md`, which `get_research_protocol` also returns.

## 6. Price indexes

Constant-dollar conversion defaults to **R-CPI-U-RS**, the index the Census Bureau uses for all ACS years. `price_index="cpi-u"` is available for comparison and `"c-cpi-u"` (chained CPI) for matching CPS ASEC income reports, which Census has adjusted with C-CPI-U from 2000 onward since 2023. Index data lives in `data/price_indexes/` with source checksums. If a year is missing, the run fails with `PRICE_INDEX_UNAVAILABLE`. Do not supply guessed values through `cpi_table`.

Years with an unpublished month (2025: October was never published) are excluded. BLS's incomplete-year figure is used only with `allow_provisional_index=True` (`allow_provisional` in `adjust_inflation`), which adds a `PROVISIONAL INDEX` advisory; say so wherever results are reported.

Home values in releases before 2022 were not on the period end-year dollar basis (ACS user note 2023-07). Plans include an advisory when such tables are deflated.

## 7. Error codes

Branch on `error_code`, not on message text: `INVALID_INPUT`, `INVALID_GEO_LEVEL`, `INVALID_OUTPUT_FORMAT`, `INVALID_DATASET`, `INVALID_VARIABLES`, `VINTAGE_UNAVAILABLE`, `PRICE_INDEX_UNAVAILABLE`, `CONCORDANCE_REVIEW_REQUIRED`, `UNVERIFIED_CONCORDANCE`, `METADATA_UNAVAILABLE`, `API_HTTP_ERROR`, `NO_DATA`.

## 8. Outputs to cite and keep

Every run writes `<table>_extraction_metadata.json`. It records the package version, git commit, metadata checksum per vintage, the concordance mode, the normalization version, the overrides and rules applied, the price index and its source, any tract harmonization, the advisories, the SHA-256 of every output file, and a `replay_command`. Keep it alongside results. `<table>_concordance.csv` maps every output column back to its code in each year.

**Replay.** `replay_extraction(metadata_path)` (CLI: `replay`) re-runs an extraction into a new folder and compares every file, then every cell. Differences caused by Census revising published data return `success=true, verified=false` with the changed cells; report them, they are not errors. Use `refresh=True` to bypass the query cache.

**Nationwide.** `state_code="all"` covers the 50 states and DC (`include_pr=True` adds Puerto Rico). County and place levels take one request per year; tracts take one per state per year. Failed calls are listed in `failed_calls`; re-running resumes from the query cache.

## 9. Derived statistics

Every derived result carries `estimate`, `moe` (90%), `se`, `cv_percent`, `method`, and `notes`. Report the method.

| Need | Tool | Rule |
| :--- | :--- | :--- |
| Rate where the numerator is a subset of the denominator (poverty rate, share 65+) | `derive_estimate("proportion")` | If the radicand is negative the ratio formula is used and `method` says `proportion_ratio_fallback` |
| Ratio of two non-nested estimates (males per female, renters per owner) | `derive_estimate("ratio")` | Never use `proportion` for non-nested quantities |
| Sum, difference, product, percent change | `derive_estimate(...)` | Sums of zero estimates include every MOE (Handbook 2020, ch. 8) |
| Combine geographies | `aggregate_geographies` | Sum counts first, then `derive`; averaging member rates is wrong. Medians, means, and percentages are refused |
| Median for a custom area | `median_from_distribution` | Sums bracket counts, then linearly interpolates as Census does. Top-coded medians return the bracket bound with `+` and no MOE |
| Many geographies tested at once | `batch_significance` | Default `holm`; `bh` for exploratory screening. Report `significant_adjusted` |

**Approximation versus exact variance.** The formulas above ignore covariance and get worse as more estimates are combined. For any derived estimate that feeds a hypothesis test, call `check_replicate_availability(table, year, geo_level)`. When Census publishes Variance Replicate Estimates (2014 onward, a listed subset of detailed tables, never medians), pass `use_replicates=True` (or `method="replicate"`): the statistic is evaluated on all 80 replicates and Var = 4/80 × Σ(rep − est)². Two cases return special values: estimates built only from controlled cells get MOE 0, and uncontrolled cells with zero replicate variance (Census uses a model-based MOE there) get MOE NaN with a note.

**Median MOEs without replicates.** The default approximation fits the Census 50-percent-proportion variance function to the published bracket MOEs. This is a documented adaptation, not a Census-published formula: it tracks published county MOEs within about 10–20%. The PUMS design-factor method is used only when you pass `design_factor`. Medians rebuilt from B19001 income brackets are usually within 1% of the published B19013 but can differ by more than 2% because Census interpolates a finer internal distribution; gross-rent medians (B25063) reproduce B25064 to the dollar.

**Comparing periods.** Comparisons of overlapping 5-year periods share sample; the z-test applies the overlap factor and notes it. Prefer non-overlapping periods (at least 5 years apart) for confirmatory tests.

## 10. Geography changes

- **Tracts across 2010/2020.** Releases through 2019 use 2010 tracts; 2020 onward use 2020 tracts. `batch_download(..., harmonize_tracts_to=2020)` or `apportion_tracts` apportion the other vintage with the Census tract relationship file (land-area weights by default; NHGIS-style population weights via `weights_file`). MOEs assume independent components and exact weights; say so. Counts only: apportion counts, then re-derive rates and medians.
- **Connecticut** switched from eight counties to nine planning regions in the 2022 release; tract GEOIDs differ between 2020–2021 and 2022+. Harmonization recodes them.

## 11. Validation against Census-published values

`run_validation` (CLI: `validate`; script: `scripts/validate_against_census.py`) recomputes values the Census Bureau publishes independently and compares: pipeline cells against raw API cells, Data Profile percentages from detailed tables, replicate MOEs against published MOEs, medians from distributions, Comparison Profile inflation adjustments and significance flags, and county sums against states. Offline checks run against frozen fixtures in every test run; CI runs the live battery weekly. If your analysis relies on a calculation path, check that the corresponding validation check passes in `docs/audits/validation_report.md`.

## 12. Inference (models, small areas, correlations, figures)

Validated in `docs/audits/inference_validation.md` by simulation with known truth (coverage of nominal 90% intervals).

| Task | Tool | Rule |
| :--- | :--- | :--- |
| Model-ready data | `build_analysis_frame` | Each variable keeps `_se` and `_cv`; `use_replicates=True` attaches Census replicates |
| Regression | `fit_model(method="replicate")` | Intervals default to `target="population"` (model SE; coverage 0.896). `target="these_areas"` uses replicate SEs for the relationship among these areas in this period (0.895). `"conservative"` adds both and is too wide (0.961) |
| Noisy predictors | `fit_model(method="regression_calibration")` | Corrects attenuation (bias removed, coverage 0.89). Replicates do not correct attenuation. SIMEX under-covers (0.81) and is not for confirmatory use |
| Noisy small areas | `small_area_estimates` (Fay–Herriot) | Reproduces the published `sae` reference to about six significant figures; EBLUP intervals cover 0.897. Report `gamma` (how far each area moved toward the model) |
| Correlations | `correlation_matrix` | Intervals cover the correlation of the observed estimates (0.895). Sampling error shrinks correlations toward zero; `disattenuate=True` reports a corrected value alongside |
| Trends | `trend_test` | Only non-overlapping releases (at least 5 years apart); overlapping sets are refused |
| Planning | `minimum_detectable_difference`, `reliability_profile` | Check whether the data can detect the effect before designing the analysis |
| Figures and tables | `make_figure`, `make_table`, or `figure_dir=` on model tools | Vector files with values CSV, caption, and provenance |

**Pre-registration.** For confirmatory work, write a manifest (`examples/manifests/`), call `create_manifest` to validate it and get its SHA-256, commit it, then `run_manifest`. Every output carries the hash; anything that could not run is listed under `deviations`, never silently substituted. Analyses outside the manifest are labeled exploratory.

**Local models.** `python mcp_server.py --profile core` exposes 13 tools with short descriptions; every result starts with `summary` and `next_step`. Call `get_recipe` first for multi-step tasks. See `docs/local_models.md`.

## 13. Geography and maps

- **Maps:** `make_map(input_path, column, level, year, se_col=...)`. Pass an SE or MOE column so unreliable areas are hatched. Report the class separability printed in the caption; when it is low, adjacent colors are not statistically distinguishable. Use `kind="significance"` with a `batch_significance` output to map change, never raw differences.
- **Boundaries** are matched to the ACS release automatically (2010-based tracts through the 2019 release, 2020-based from 2020; Connecticut planning regions from 2022).
- **Spatial statistics:** `spatial_autocorrelation` (Moran's I, optional local clusters with correction), `spatial_regression` (lag or error), `spatial_small_area_estimates` (spatial Fay–Herriot). Noisy small-area estimates can create or hide clusters: pass replicates, or rerun on Fay–Herriot estimates as a sensitivity check.
- **Tract harmonization** uses decennial block population or housing-unit weights by default (`weight="auto"`); the first national run downloads about 3,200 county block-count requests, then caches them.
- **Custom areas:** `custom_area_mapping(areas_path, ...)` then `aggregate_geographies`. Partial overlaps are reported with their allocation assumption.
- **Errata:** every extraction plan lists Census errata and user notes for the table and releases; mention them in write-ups.
- **Controlled estimates** (Census MOE code -555555555) now keep MOE 0 with a `<code>_CONTROLLED` flag column.

