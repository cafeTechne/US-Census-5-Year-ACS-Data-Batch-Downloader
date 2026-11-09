# Changelog

All notable changes, by development round. Versions follow `census.__version__`;
each tagged release (`v<version>`) is archived on Zenodo with its own DOI (see
[Releasing](#releasing)). Plans and acceptance criteria for each round are in
`PLAN_ROUND_<n>.md`.

## 0.15.0 (Round 16): spatial methods validated under ACS sampling error

- Project renamed **Census Research Engine** (github.com/cafeTechne/Census-Research-Engine);
  the distribution name `census-acs-toolkit` is unchanged.
- **Lag-model effects:** `spatial_regression(model="lag")` reports direct, indirect and
  total effects first (delta-method and replicate SEs; verified against the S(W) algebra
  and spreg's multipliers), with notes that beta is not the marginal effect and that a
  rho = 0 test is not a specification test. Coverage and a heavy-tail caveat at high rho
  are measured (`docs/audits/spatial_regression_validation.md`).
- **New option `spatial_regression(model="error", correct_outcome_error=True, se_col=...)`:**
  corrects lambda for outcome sampling error using spatial Fay-Herriot (REML). Lambda
  coverage is 0.86-0.93 against 0.003-0.85 uncorrected, measured on the shipped estimator.
  - Assumes errors independent across areas. Warns when errors are correlated, and when
    the outcome is a controlled total, where the sign of the bias depends on the release.
  - Limited to 2,000 areas.
- **ACS sampling-error covariance measured from VRE replicates** (Michigan and New York,
  2015-2019 and 2020-2024; `docs/audits/acs_sampling_covariance.md`):
  - neighbour error correlation is about 0 for rates, shares, medians and most counts;
  - controlled totals are correlated within county, with a sign that can differ by
    release.
- **LISA validation:** under ACS error (`docs/audits/lisa_validation.md`), with no default
  change.
  - The Round 15 "38%" caveat is corrected: it counts maps with at least one significant
    area, not contiguous false clusters.
  - Replicate stability and Fay-Herriot reruns are shown not to be false-positive checks.
- **Common-factor and specification studies:** LR size measured in Juhl's design; Koley &
  Bera score tests shown not to identify SEM vs SDM. All new studies are info-only and
  run on demand.
- **Polite census.gov downloads:** one shared helper (`census/http.py`) with
  - backoff and Retry-After on 429/5xx;
  - per-host pacing;
  - a clear error for the firewall "Request Rejected" page, with the support ID and the
    cache path for a manual download;
  - validation of manually placed files.
- **Literature:** full-text reading notes with verification levels
  (`docs/literature/full_text_reading_notes.md`). Remote-access planner decisions are
  recorded in `docs/remote_access_decisions.md`.
- **CI:** the weekly live-validation cron moved off the hour (Mondays 06:17 UTC).

## 0.14.0 (Round 15): studies across releases, spatial coverage, 2020 Census

- Confirmatory studies across releases: CPI conversion to a declared `dollar_year`, then
  each release's own RPP (`rpp_years`); `"replicates": {"source": "census_vre"}` fetches
  Census VRE tables (URLs and SHA-256 recorded). Overlapping releases are refused for any
  replicate source; `replicate_scope: "covariates_only"` is an explicit, hashed exception
  for outcomes without replicates. Example: MI/NY change in CPI- and RPP-adjusted mean
  household income, 2015-2019 to 2020-2024: +$3,111 per county, 90% CI [2,709, 3,513].
- Spatial coverage studies (info): `spatial_regression_coverage`, `local_morans_size`.
  **Default changes (owner decisions):** the spatial lag model now defaults to GMM (ML rho
  coverage under ACS sampling error was 0.48 at rho 0.3 and 0.003 at 0.6; GMM 0.89-0.90);
  local Moran's I now reads p-values two-sided with 9999 permutations (per-area false
  positive rate 0.20 to 0.10). Previous behaviour: `method="ml"`;
  `sided="folded", permutations=999`. Caveats with measured numbers in both tools'
  outputs (error-model lambda attenuation; false clusters under spatially patterned
  sampling error).
- spreg's per-fit stdout print is suppressed at its source, so it can no longer corrupt
  the MCP stdio stream.
- 2020 Decennial Census: P.L. 94-171 (`dec/pl`) and DHC (`dec/dhc`) through the existing
  pipeline. Counts are "not sample-based (MOE undefined)", never MOE 0, with a
  disclosure-avoidance advisory; sampling tests refuse decennial-only input; ACS vs
  census comparisons treat the count as a fixed reference. New live check F11
  (invariants and block-to-tract/county hierarchy, 5,492 values); 11/11 checks pass live.
- Remote access decisions recorded (`docs/remote_access_decisions.md`); no networking
  changes in this release.

## 0.13.0 (Round 14): confirmatory multi-state analyses

- Pre-registered manifests take several states (`geography.states`, 2-digit FIPS; not
  with ZCTAs), derivations evaluated on replicates, and a declared RPP adjustment step
  (`adjustments`, shared with `place_adjust_frame`; `rpp_year` required, filled in by
  `create_manifest` into `<name>.completed.json` when omitted; exclusions run first;
  RPP provenance recorded). The Michigan/New York study runs confirmatorily
  (`examples/manifests/mi_ny_income_education.json`) and reproduces Round 13 exactly
  (pct_bachelors 811.0, 90% CI [684.3, 937.7]).
- Validation battery adds Michigan (26); all 10 checks pass live and offline.
- PUMS memory measured (`scripts/pums_memory_benchmark.py`, `docs/audits/pums_memory.md`;
  risk R2 closed): peak about 275 MiB + 2.8 KB per person record. Avoidable weight-matrix
  copies removed (1.9M rows: 6.4 to 5.4 GiB peak, results bit-identical); grouped medians
  gather each group once.
- MCP `cache_import` (full profile) with the CLI's safety rules; pandas 4 deprecation in
  `format_dataframe` fixed.
- Review fixes: `run_manifest` records replicate-derivation errors as deviations instead
  of raising; ACS refs with E/PE suffixes match their replicates; methods text reports how
  many units received replicate SEs.

## 0.12.0 (Round 13): real data, end to end

- Live verification: the full battery against Census-published values passes live (10/10
  checks, `docs/audits/validation_report.md`); stale F5 expected-failure marker and
  criterion text fixed (the first dispatched CI live run failed only on that marker).
- Live case study for Michigan and New York counties (`docs/case_study_mi_ny.md`): RPP-adjusted
  median household income, journal maps with an uncertainty layer, a regression with
  replicate refits, a results bundle; CLI only. New tools `place_adjust_frame` (county RPP by
  CBSA membership, applied to estimates, SEs, and replicates) and `make_results_bundle`;
  `build_analysis_frame` takes several input and replicate files; `tool ... --save PATH`.
- The Round 12 reference workflow runs entirely from the CLI: `python -m census cache import`
  (atomic; a synthetic import into the default cache needs `--allow-default-cache`;
  `cache clear` also removes the synthetic marker) and `run_manifest --arg replicates_path=...`
  with Python/CLI/MCP parity tests.
- Provenance: extraction metadata records the observed data source; bundles take it from
  their inputs (`census_api`, `synthetic`, `mixed` with a warning, or `unknown`), not from
  the cache present at bundle time. Failed saved results are refused.
- Bundled price index and RPP tables are verified against their SHA-256 at load (risk R1
  closed); custom tables are recorded with their hash and marked unverified. New error
  code `BUNDLED_DATA_INTEGRITY`.
- Validation: a fast smoke gate (band widened by 3 Monte Carlo SE) runs on pull requests;
  the full gate is a release step and now runs in parallel (`--jobs`), with BLAS pinned to
  one thread, which also makes its results independent of the machine's thread count.
  New info-only study `did_ar1_sensitivity`: DiD coverage stays near nominal under AR(1)
  errors up to rho 0.9 in 4-5 period panels.
- Fixes: map legends stay inside journal-width figures and do not split identifiers;
  `_load_frames` rejects missing GEOID/Year keys; pandas-3 integer replicate columns.

## 0.11.0 (Round 12): verifiable and reproducible workflows

- Independent dense reference checks for the multilevel GLS layer and two-group DiD.
- `scripts/inference_validation.py --gate`; `docs/audits/CHECK_MAP.md`. The gate also
  requires the default multilevel and DiD coverage studies (`multilevel_known_variance`,
  `multilevel_random_slope`, `did_two_group`, `did_event`); replicate-refit and
  serial-correlation variants remain on demand.
- `census.results.interval_description`: every output (tables, figures, methods,
  tool and adapter results) states the same estimand, interval basis, level, and
  distribution; method-aware estimand text (PUMS, DiD, area-level); undefined
  uncertainty shown as undefined.
- `docs/reference_workflow.md`: an offline, rerunnable reference analysis with a
  blocked case, checked by an independent second reader.
- Manifests: `confirmatory_incomplete` for runs with blocking deviations (including
  failed calls); synthetic labels follow the actual data source, with mismatch
  deviations; caller-supplied replicates described and hashed identically across
  pandas/numpy versions; hypothesis results kept in bundles; `run_manifest` tool
  accepts replicates and returns the run status.
- CLI tool runner accepts `--arg KEY=VALUE` (portable paths).

## 0.10.1 (Round 11): audit decisions

- **Q2 incomplete replicates:** `replicate_policy="flag"|"strict"` on replicate-based
  models; pre-registered manifests run strict; exploratory results say when sampling
  SEs may be understated.
- **Q1 DiD serial correlation:** coverage measured under AR(1) area shocks (0.896-0.905
  at rho 0.5 and 0.8, default population target); no estimator added; assumption
  documented in `census.did`, with a not-validated warning for panels over 4 periods.
  The non-default these-areas target is conservative under serial correlation
  (coverage 0.90-0.965).
- **R2 PUMS memory:** measured ~3.6 GB peak at California scale; warning for all-column
  reads of large files.
- **R1 price-data integrity:** documented guarantee (verified at generation and by tests,
  not at each load).
- pandas 3 read-only array fixes in `census.models`, `census.did`, `census.analysis`.

## 0.10.0 (Round 10): microdata, 1-year estimates, research designs; Round 11 audit fixes

- **PUMS microdata** (A, B): `census.pums` tabulations, crosstabs, weighted
  medians, and survey-weighted regression with 80 replicate weights; reproduces
  all 32 Census PUMS verification estimates and SEs for Wyoming (2024 1-year and
  2020-2024 5-year). PUMA boundaries.
- **ACS 1-year** (C): `product="1-year"` across planning and extraction;
  2005-2025 metadata archive (no standard 2020 release); 65,000+ geography checks;
  1-year vs 5-year comparisons refused; product-aware overlap.
- **Designs** (D): `census.multilevel` (Kenward-Roger intervals) and `census.did`
  (non-overlapping periods, full replicate refits); simulation coverage 0.89-0.91.
- **Regional price parities** (E): `census.rpp`, BEA RPPs for states, metro and
  nonmetro portions, and MSAs; never applied by default.
- **Carryovers** (F): ZCTA and place crosswalks, self-contained interactive HTML
  maps, local-model harness tasks for maps and spatial tools.
- **Round 11 audit fixes** (C1-C7 in `PLAN_ROUND_11.md`): undefined PUMS
  replicates no longer report zero uncertainty; zero-weight records no longer move
  weighted medians; spatial replicates are aligned to the estimate's release;
  scalar SE/CV decode Census sentinels; two-group DiD requires a pre-treatment
  reference; regression outputs name the SE behind each interval.
- Concept DOI for citation: [10.5281/zenodo.23925301](https://doi.org/10.5281/zenodo.23925301).

## 0.9.1 (hotfix): Zenodo metadata

- Zenodo archiving: add `.zenodo.json` with an explicit creator name (Zenodo could not build creators from the alias-only `CITATION.cff` author), so releases are archived and receive a DOI. No code changes.

## 0.9.0 (Round 9): geography, maps, spatial inference, data quality, packaging

- **Geography and maps** (workstreams A-E): cartographic and TIGER/Line boundaries
  with vintage-checked joins, static journal maps that report class separability
  and mark low-reliability areas, spatial statistics and models that carry ACS
  sampling error, block-population-weighted crosswalks, block groups and custom areas.
- **ACS errata and user notes** (F): `census.errata` and `data/acs_errata.json`,
  generated by `scripts/fetch_acs_errata.py` from the Census errata and user-notes
  pages (source URL and retrieval date per record). `errata_for(table, years)`
  looks up notes that name a table; `check_urls` confirms every cited note resolves.
- **Controlled-estimate MOEs** (G1): `clean_census_sentinels` now keeps a
  controlled MOE (`-555555555`) as `0` and adds a boolean `<base>_CONTROLLED`
  column (behavior change; `preserve_controlled=False` restores NaN). Validation
  check F1 accepts exactly this representation.
- **Slim core download** (G2): `workflows.download_table(table_code, geography,
  state_code, years, variables, output_dir)` plans, stops if the plan would block,
  then downloads a panel with codebook and SE/CV columns. It replaces
  `batch_download` in the `core` MCP profile (the full profile keeps it). Round 7
  workflow tools return native `summary` and `next_step`; `run_validation`'s
  per-check tally moved from `summary` to `checks_summary`.
- **Tract CV profile** (G3): the simulation tract profile is derived from real
  2023 tract MOEs (`tests/fixtures/cv_profile`, `simulation.derive_cv_profile`)
  instead of an assumed one.
- **Packaging** (H): `pyproject.toml` (`pip install -e .` from a clone, extra `dev`), console
  scripts `census-acs` and `census-acs-mcp`, this changelog, a tag-triggered
  release workflow, and `CITATION.cff` 0.9.0.

## 0.8.0 (Round 8): inference that respects ACS sampling error

Analysis frames with SE columns and replicates; replicate-refit regression
(OLS/WLS/GLM), regression calibration, SIMEX and reliability screening;
Fay-Herriot small-area estimation (reproduces the R `sae` example); power and
minimum detectable differences; GLS trend tests on non-overlapping releases;
correlation matrices; journal figures and tables; preregistration manifests;
MCP tool profiles (`core`, `review`, `full`), recipes, and coverage simulations.

## 0.7.0 (Round 7): derived statistics verified against Census

Proportions, ratios, products, percent changes, aggregation of geographies,
medians from distributions, all with 90% MOEs; Variance Replicate Estimates for
exact variances; batch significance with multiple-comparison corrections; the
F1-F10 validation battery against Census-published values; table-scoped
concordance rules; the 2010/2020 tract crosswalk; nationwide extraction;
extraction replay; C-CPI-U; `CITATION.cff`; continuous integration.

## Round 6: vintage-safe variables and R-CPI-U-RS

Cross-release variable concordance (concept IDs, reviewed overrides, continuity
checks that block unsafe panels), metadata archive per vintage, constant dollars
with R-CPI-U-RS (the Census Bureau's ACS index), and an agent-first interface.

## Round 5: research-grade pipeline

Tidy GEOID x Year panels, Parquet export, codebooks, sentinel cleaning with
notes, SE/CV/reliability columns, inflation adjustment, and a query cache.

## Round 4: MCP server

FastMCP server exposing table search, variable lookup, and batch download to agents.

## Round 3: headless library

The `census` package: API client, geography, tables, export, and statistics
separated from the GUI, with a command-line interface.

## Round 2: research usability

Improvements to the desktop downloader for research workflows (table search,
multi-year batches, output formats).

## Round 1: foundation

Initial stabilization of the original batch downloader.

## Releasing

1. Bump `census/__init__.py` `__version__` (and `PACKAGE_VERSION` in
   `census/pipeline.py`), update this file and `CITATION.cff` (`version`,
   `date-released`).
2. Tag and push: `git tag v0.9.0 && git push origin v0.9.0`. The `Release`
   workflow (`.github/workflows/release.yml`) builds the sdist and wheel, checks
   the tag matches the package version, and creates a GitHub release with both
   files attached.
3. Zenodo: the repository owner enables the repository once in Zenodo
   (Account > GitHub > toggle the repository on). Zenodo then archives every
   published GitHub release and mints a version DOI plus a concept DOI that
   always resolves to the latest version. After the first release, add the
   concept DOI to `CITATION.cff` (`doi:`) and the README badge.
