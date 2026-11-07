# PLAN_ROUND_10.md: Microdata, Timelier Estimates, and Research Designs

**Planning Round:** 10
**Date:** 2026-11-03
**Status:** Implemented (section 13); audit fixes from PLAN_ROUND_11.md C1-C7 included
**Branch:** `phase10/microdata-and-designs` (from `defaultBranch` after the Round 9 merge)

---

## 1. Summary

Rounds 5–9 built a verified path from published ACS tables to maps and models. Three limits remain for research:

1. **Published tables fix the question.** If no table crosses the variables a hypothesis needs (for example rent burden by disability status by age), the toolkit cannot answer it. The Public Use Microdata Sample (PUMS) can, with its own 80 replicate weights.
2. **5-year estimates are slow and blur change.** ACS 1-year estimates (areas of 65,000+ people) are timelier and do not overlap; they suit trend and event analyses for large places.
3. **Designs, not just models.** Common causal and comparative designs on ACS data (difference-in-differences across non-overlapping periods, multilevel models of tracts within counties) need sampling error handled correctly and are easy to get wrong.

Round 10 adds PUMS with replicate-weight inference, ACS 1-year support, cost-of-living adjustment between places, multilevel and difference-in-differences designs, and the Round 9 carryovers.

| Priority | Workstream | Section |
| :--- | :--- | :--- |
| P0 | A. PUMS microdata with replicate-weight variance | 3 |
| P0 | B. Validation of PUMS tabulations against published tables | 4 |
| P1 | C. ACS 1-year estimates | 5 |
| P1 | D. Research designs: multilevel models, difference-in-differences | 6 |
| P1 | E. Cost-of-living adjustment between places (BEA RPP) | 7 |
| P1 | F. Round 9 carryovers | 8 |
| P2 | G. Release, DOI, and documentation site | 9 |

---

## 2. Principles carried forward

Census documentation is the specification; every result records its method and the uncertainty sources in its SEs; nothing is accepted without validation against published Census values or simulation with known truth; tools work for local models through the core profile.

---

## 3. Workstream A (P0): PUMS microdata

- **Source:** ACS PUMS person and housing files (1-year and 5-year) from `www2.census.gov/programs-surveys/acs/data/pums/`, and the PUMS API (`api.census.gov/data/<year>/acs/acs5/pums`) for targeted pulls. Data dictionaries per release (variable codes and value labels change across releases, as table codes do; the Round 6 concordance approach applies).
- **Weights:** person weight `PWGTP` with replicates `PWGTP1`–`PWGTP80`; housing weight `WGTP` with `WGTP1`–`WGTP80`. Variance by successive difference replication, `Var = 4/80 Σ (θ_r − θ)²`, exactly as for table replicates (`census.variance`).
- **Tabulation engine:** weighted counts, means, proportions, medians (weighted quantiles with the Census PUMS median method), ratios, and crosstabs, each with replicate SEs and the `DerivedEstimate` fields.
- **Geography:** Public Use Microdata Areas (PUMAs, 100,000+ people); PUMA boundaries per vintage (2010 PUMAs through the 2021 releases, 2020 PUMAs from 2022), PUMA-to-county/tract relationship files for context; maps through Round 9.
- **Models on microdata:** survey-weighted regression with replicate-weight SEs (refit on each replicate weight), reusing the Round 8 `ModelResult` contract.
- **Disclosure caution:** PUMS is public but individual-level; outputs are aggregates only, and small cells are flagged (unweighted n below a threshold).

## 4. Workstream B (P0): PUMS validation

Census publishes verification files for every PUMS release: selected estimates and their replicate SEs computed from the same PUMS. Our tabulations must reproduce them.

- Reproduce the Census **PUMS verification estimates** (estimate and SE) for the latest 1-year and 5-year releases.
- Reproduce the worked examples in the **PUMS Accuracy of the Data** statements (used in Round 7 for the median method).
- Compare PUMS-derived totals with the corresponding published tables for large geographies, with tolerances reflecting the PUMS sampling of the full ACS.

## 5. Workstream C (P1): ACS 1-year estimates

- Datasets `acs1`, `acs1/profile`, `acs1/subject`, `acs1/cprofile`; per-vintage metadata archive extended to 1-year releases (2005 onward), with the Round 6 concordance.
- **Geography rules:** published only for areas of 65,000+; the planner reports which requested areas exist.
- **2020 experimental estimates:** the 2020 1-year release was replaced by experimental estimates with different methods; treat 2020 as unavailable by default with an advisory and an explicit opt-in.
- **Comparisons:** 1-year periods do not overlap, so the overlap adjustment is 0; comparing 1-year with 5-year estimates is refused (Census advises against it).
- **Replicates:** 1-year variance replicate tables are used where published.

## 6. Workstream D (P1): research designs

- **Multilevel models** (tracts within counties, people within PUMAs): `statsmodels` MixedLM with known sampling variance at level 1 (a Fay–Herriot-style likelihood), random intercepts and slopes, and replicate refits for sampling SEs.
- **Difference-in-differences** on non-overlapping periods: two-group and event-style designs using area-level estimates with known variances; the overlap check from Round 8 enforced; parallel-trends diagnostics across pre-periods where at least three non-overlapping periods exist; replicate-based SEs where available.
- **Validation:** simulation coverage (88–92% for nominal 90%) and reproduction of a published reference example for each design.

## 7. Workstream E (P1): cost-of-living adjustment between places

- **BEA Regional Price Parities** (RPP) by state and metropolitan area, file-backed with checksums like the price indexes.
- `adjust_for_place(value, geography, year)` converts nominal dollars to national-average purchasing power; combined with R-CPI-U-RS for time, giving dollars comparable across places and years.
- Advisory when an area falls outside RPP coverage (nonmetropolitan portions use state nonmetro RPPs) and a note that RPPs carry their own estimation error that is not in ACS MOEs.

## 8. Workstream F (P1): Round 9 carryovers

1. Local-model evaluation tasks for maps and spatial tools (`make_map`, `spatial_autocorrelation`); decide whether `make_map` stays in the core profile from the results.
2. ZCTA and place crosswalks from block assignment files (block-to-ZCTA and block-to-place), using the Round 9 block weights.
3. Interactive HTML map export, self-contained with no external tile servers (P2 in Round 9).
4. A second local model in the evaluation (another open-weights family at a similar size) to separate model limits from tool-design problems, especially for the recipe task.

## 9. Workstream G (P2): release and documentation

- First tagged release (v0.9.0 or v0.10.0) once the Zenodo integration is enabled, with the concept DOI added to `CITATION.cff` and the README.
- Optional documentation site built from the Markdown docs and recipes.

---

## 10. Out of scope (later)

- Decennial census tables beyond the block counts used for crosswalks.
- Restricted-use microdata (Federal Statistical Research Data Centers).
- Bayesian hierarchical models beyond Fay–Herriot and multilevel likelihoods.

---

## 11. Decisions

| # | Question | Decision |
| :--- | :--- | :--- |
| 1 | PUMS access | **Decided:** download full state files on demand (cached, checksummed) for tabulation; use the PUMS API only for small targeted pulls |
| 2 | Small-cell flag | **Decided:** flag estimates from fewer than 30 unweighted records and any with CV > 30%; do not suppress, since PUMS is already public |
| 3 | 2020 1-year experimental estimates | **Decided:** unavailable by default; opt-in with an advisory |
| 4 | Multilevel and DiD SEs | **Decided:** same interval targets as Round 8. Exception by design: `pums_regression` defaults to `target='these_areas'` (replicate-weight SEs), because microdata regression estimates the coefficient in the surveyed population (the state or nation), which is the design-based inference Census recommends for PUMS; area-level models keep `population` as the default (population by default, these-areas with replicates), validated by simulation before becoming defaults |
| 5 | Cost-of-living adjustment | **Decided:** optional and never applied by default; results state when RPPs were used |

---

## 12. Acceptance criteria

1. PUMS tabulations reproduce Census verification estimates and SEs for at least one 1-year and one 5-year release.
2. PUMS replicate-weight regression SEs match a hand-computed replicate formula and pass simulation coverage.
3. 1-year extractions report unavailable geographies and refuse 1-year vs 5-year comparisons.
4. Multilevel and DiD designs meet coverage targets in simulation and reproduce a published reference example.
5. RPP adjustment reproduces BEA published real personal income for at least two areas.
6. The Round 7 and Round 8 validation batteries still pass; CI green on Linux and Windows, Python 3.10–3.12.

---

## 13. Implementation status

| Workstream | Result |
| :--- | :--- |
| A PUMS | `census/pums.py`: download, dictionaries, tabulations, crosstabs, weighted medians, replicate-weight regression; PUMA boundaries |
| B PUMS validation | `census/pums_validation.py`: all 32 Census verification estimates and SEs reproduced for Wyoming (2024 1-year, 2020-2024 5-year), max SE error 0.498 (published values are whole numbers) |
| C ACS 1-year | acs1 datasets 2005-2025 (no standard 2020 release; `/2020/acs/acs1` returns 404); 26 MB metadata archive; 65,000+ availability probe; mixed-product comparisons refused; no 1-year replicate tables exist |
| D Designs | `census/multilevel.py` (Kenward-Roger/Satterthwaite intervals, coverage 0.891/0.890), `census/did.py` (coverage 0.909 two-group, 0.902 event and these-areas after full replicate refits); references verified against the statsmodels documentation page and Card and Krueger (1994) |
| E Regional price parities | `census/rpp.py`: BEA SARPP/PARPP/MARPP 2008-2024; BEA real personal income reproduced for all states in 2008 within 2e-5 (requires a common per-year scaling BEA does not document; recorded in the module) |
| F Carryovers | ZCTA and place crosswalks; `census/web_maps.py` self-contained HTML maps; evaluation harness tasks for `make_map` and `spatial_autocorrelation` |
| Interfaces | `census/workflows_microdata.py`: six tools (57 total); `product` and `allow_experimental_2020` on extraction tools |
| Audit | `PLAN_ROUND_11.md` C1-C7 fixed with 28 tests (`tests/test_audit_r11.py`) |

**PUMS geography note:** per the PUMS data dictionaries, the 2018-2022 5-year PUMS codes PUMAs in two columns (`PUMA10` for data years 2018-2021, `PUMA20` for 2022); the 2019-2023 and 2020-2024 5-year files use 2020 PUMAs throughout. `puma_vintage(2022, "5-year")` refuses the mixed release, as for 2012-2015.

**Not done and carried forward:** the two-model local evaluation (the run was interrupted by a usage limit, and the Linux workstation has no Ollama; the harness and tasks are ready: `python scripts/local_model_eval.py --model qwen3:8b llama3.1:8b`). `make_map` remains in the core profile untested with local models until that run. Round 11 owner decisions R1, R2, Q1, Q2 (D6-D8) remain open.

