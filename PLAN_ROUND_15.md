# Round 15: Studies Across Releases, Spatial Coverage, 2020 Census

**Date:** 2026-11-08
**Status:** Authorized (owner and planner decisions, 2026-11-08); implementation in progress
**Baseline:** v0.13.0
**Companions:** [PLAN_ROUND_14.md](PLAN_ROUND_14.md), [docs/remote_access_decisions.md](docs/remote_access_decisions.md)

## 0. Front-loaded findings and boundaries

| ID | Source | Finding | Disposition |
| :--- | :--- | :--- | :--- |
| G1 | Round 14 deferrals | A manifest's RPP adjustment covers one release only, and `run_manifest` cannot fetch Census VRE replicates itself | Workstream A |
| G2 | `docs/audits/CHECK_MAP.md` | `spatial_regression` (lag/error) and `local_morans` have no coverage study; global Moran's I has only an info study | Workstream B |
| G3 | Owner gap analysis | ACS only. 2020 Decennial Census access exists only internally, as block counts for crosswalks (`census/block_weights.py`) | Workstream C |
| G4 | Scheduled CI | First weekly live run was due 2026-11-09 | Check it and triage as in Round 13 A1 |

**Boundary (planner):** no networking changes in this round, and no HTTP, persistent service, job queue, custom transfer API or Data Commons adapter. Remote access is the next round (SSH, per `docs/remote_access_decisions.md`).

As before: no tuning of tolerances, bands or thresholds to make something pass.

## 1. Workstream A: confirmatory studies across releases (P0)

- **A1. Several releases with price adjustment.** A manifest with several releases (for example non-overlapping 5-year periods, 2015-2019 vs 2020-2024, or 1-year releases) can declare RPP adjustment for each release's own year. Dollars are converted to one declared dollar year with the existing CPI machinery (R-CPI-U-RS default), *then* place-adjusted. The order is stated in the methods text.
  - The declared `dollar_year` and per-release `rpp_year` are required, so the hash fixes them; `create_manifest` fills them as in Round 14.
  - Existing overlap rules for comparisons of overlapping 5-year periods stay in force.
- **A2. Automatic replicate retrieval.** `"replicates": {"source": "census_vre"}` in a manifest makes `run_manifest` fetch the VRE tables for the manifest's variables, geography and releases, using the existing `census.replicate` code and its cache.
  - The VRE file hashes and URLs go into the run record.
  - Variables with no published replicates (for example B19013) are reported. Under the strict confirmatory policy they block, as today, unless the manifest says which analyses use only covariate replicates. The rule is explicit, never silent.
- **A3. Example study and tests.**
  - An example manifest for a change study in Michigan and New York counties across two non-overlapping 5-year periods: change in RPP- and CPI-adjusted median household income, or another pre-registered contrast the existing methods support.
  - Frozen-fixture offline test, plus a live test gated by `CENSUS_LIVE_TESTS=1`.
  - Docs: the preregistration recipe and a short section in the case study.

## 2. Workstream B: coverage for spatial regression and local Moran's I (P1)

- **B1. `spatial_regression_coverage`.** A spatial lag and an error model on a lattice with ACS-like sampling error, for each estimator the tool offers (GMM/ML). Report coverage of beta, and of rho/lambda, for the tool's default intervals. Also report bias and the LM diagnostics' size under the null.
- **B2. `local_morans_size`.** Local Moran's I under spatial independence plus sampling noise. Report:
  - per-area false-positive rate and family-wise or FDR control, for the tool's default inference and correction;
  - power under one clustered alternative.
- **B3. Status.** Both run first as `info` studies.
  - A study whose default intervals pass the band can be promoted to gated by owner decision (D15-1).
  - If a default fails, the tool's outputs get a visible caveat, or the default changes (for example to a corrected method), with evidence. Never tune the band.
- Update `docs/audits/CHECK_MAP.md`. Keep each study's runtime reasonable under `--jobs`.

## 3. Workstream C: 2020 Decennial Census (P1, provisional)

- **C1. Retrieval.** P.L. 94-171 (`dec/pl`) and the Demographic and Housing Characteristics file (`dec/dhc`) through the existing API client, cache and output formats, with variable metadata and codebook. Reuse the block-count code where possible.
- **C2. Correct uncertainty semantics.** Decennial values are **published counts that are not sample-based but not error-free**:
  - no sampling MOE;
  - coverage error, processing error, and disclosure avoidance through the TopDown differential-privacy algorithm.

  Concretely:
  - Outputs never carry a zero MOE or a CV of 0 presented as precision. The uncertainty fields are explicitly "not sample-based (MOE undefined)", with a disclosure-avoidance advisory.
  - The advisory notes that small geographies and detailed cells are noisier, that only certain counts are invariant (state total population, block-level housing units and occupied group-quarters facility counts), and that block-level characteristics should be aggregated before use.
  - Significance tools refuse decennial-only "sampling" tests, or say exactly what they test.
  - Mixing ACS and decennial values in one comparison is allowed only with a stated note.
- **C3. Validation.** A new battery check against the Census Bureau's own invariants and hierarchy:
  - state total population matches the published state totals;
  - block-level housing-unit counts sum exactly to tract and county totals;
  - tract and county totals from DHC agree with P.L. 94-171 where both publish the same item.

  Record live, freeze fixtures, keep fixtures small (Michigan, New York, Wyoming).
- **C4. Scope.** Retrieval, semantics and validation only. No new estimators. DHC tables used in the examples: P1, P12, H1, H3. The 2010 Census is out of scope unless needed.

## 4. Release

v0.14.0 with a Zenodo DOI: code review (`/code-review high`), fixes, full gate (`--jobs 0`), PR, CI, live dispatch, merge, tag, Zenodo confirmation.

## 5. Defaults taken (owner may override)

| ID | Default |
| :--- | :--- |
| D15-1 | New spatial studies start as `info`. Promotion to gated is an owner decision after the evidence is in |
| D15-2 | Multi-release dollar conversion: CPI to the declared dollar year first, then the RPP of each release's own year |
| D15-3 | Decennial geography levels: nation, state, county, tract, block group, block (block only for P.L. 94-171 and invariant counts), place |
