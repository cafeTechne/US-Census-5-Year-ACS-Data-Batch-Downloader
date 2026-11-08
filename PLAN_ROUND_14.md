# Round 14: Confirmatory Multi-State Analyses

**Date:** 2026-11-08
**Status:** Implemented (section 9); released as v0.13.0
**Baseline:** v0.12.0 (Round 13 merged)
**Companion:** [PLAN_ROUND_13.md](PLAN_ROUND_13.md) (section 11 lists the deferrals this round takes up)

## 0. Front-loaded findings

| ID | Source | Finding | Disposition |
| :--- | :--- | :--- | :--- |
| G1 | Round 13 E1 | The Michigan/New York case study is labelled *exploratory*. A `run_manifest` geography holds a single state, and there is no place-adjustment step, so the study cannot be pre-registered | Workstream A |
| G2 | Round 13 deferrals | The validation battery covers states 06, 36, 48, 17 and 56. Michigan, one of the owner's preferred test states (D13-2), is not included | Workstream B |
| G3 | PLAN_ROUND_11 R2/F3 | PUMS memory has only a static bound (one 81 x 1.9M float64 weight matrix, about 1.15 GiB). Nothing has been measured | Workstream C |
| G4 | Round 13 CI logs | Pandas 4 deprecation (`inplace` in `DataFrame.rename`, `census/export.py:40`). `cache import` has no MCP route | Workstream D |
| G5 | Scheduled CI | The first weekly live-validation run is due 2026-11-09 | Confirm it after the round lands; triage it as in Round 13 A1 |

Same rules as before: tolerances, bands and thresholds are not tuned to make a check pass. Every failure is classified as a defect, a Census revision, rounding, or flakiness.

## 1. The improvement this round delivers

A researcher can pre-register a multi-state, price-adjusted analysis as a manifest and run it confirmatorily from the CLI or MCP, with the same blocking deviations, replicate policy and provenance as the single-state route. The Michigan/New York study then becomes the worked confirmatory example. The validation battery covers Michigan, and PUMS memory use is measured rather than estimated.

No new estimator families or data providers. PLAN_ROUND_12 sec. 10 deferrals still apply.

## 2. Workstreams

| Priority | Workstream | Owner |
| :--- | :--- | :--- |
| P0 | A. Multi-state manifests with a place-adjustment step | agent A |
| P1 | B. Michigan in the validation battery | agent B |
| P1 | C. PUMS memory benchmark (R2) | agent C |
| P2 | D. Small items: pandas 4 deprecations, MCP `cache_import` | agent C (after C) |

A, B and C touch disjoint modules (`manifest`/workflows, `validation`/fixtures, `pums`/scripts) and run in parallel in separate git worktrees.

## 3. Workstream A: confirmatory multi-state analyses (P0)

- **A1. Several states in `geography`.** Accept `"states": ["26", "36"]` as an alternative to `"state"`. A manifest gives one of the two, never both. The extraction runs per state and stacks the results. The manifest hash, validation errors and run status behave as they do for one state. Single-state manifests and their hashes are unchanged; the hash of the existing example manifest must not change.
- **A2. Place adjustment as a declared step.** For example `"adjustments": [{"type": "rpp", "variables": [...], "rpp_year": 2023}]`. It reuses the Round 13 `place_adjust_frame` core (CBSA membership, nonmetro fallback, replicate scaling), not a second implementation. RPP provenance is recorded in the manifest output, including the custom-table hash when a custom table is used.
- **A3. Derived variables** if the manifest cannot already express `pct_bachelors` from B15002 cells: reuse the existing `derive` machinery and its replicate evaluation.
- **A4. Example and test.**
  - Add `examples/manifests/mi_ny_income_education.json` (pre-registered hypothesis: positive pct_bachelors slope on RPP-adjusted income).
  - Add a frozen-fixture offline test of the whole route, and a `CENSUS_LIVE_TESTS=1` live test.
  - Update `docs/case_study_mi_ny.md` so the confirmatory manifest route comes first, with the exploratory route kept as the sensitivity analysis.
  - Done when the live confirmatory run reproduces the Round 13 coefficient (811.0, 90% CI [684.3, 937.7]) or explains any difference.

## 4. Workstream B: Michigan in the validation battery (P1)

Add `"26"` to the battery's states, re-record the frozen fixtures live (`--record`), and report how much the fixture set grows. Rerun the battery live and offline. A new failure is triaged before anything else; no tolerance changes. Regenerate `docs/audits/validation_report.md`.

## 5. Workstream C: PUMS memory benchmark (P1)

Add `scripts/pums_memory_benchmark.py`, using bounded synthetic PUMS-shaped CSVs only (no downloads):
- sizes 100k, 500k and 1.9M person rows;
- peak RSS per phase (download or read, parse, concatenate, weight matrix, tabulation);
- several chunk sizes, with identical estimates across chunk sizes verified;
- platform and dependency versions recorded.

Write the results to `docs/audits/pums_memory.md`, with a documented limit and a recommendation. Change code only if the measurement shows a defect, such as a needless copy. No streaming rewrite unless the owner approves one.

## 6. Workstream D: small items (P2)

- Fix pandas 4 deprecations found by running the suite with `-W error::FutureWarning` and the Pandas4Warning class.
- Add a `cache_import` MCP tool (full profile) wrapping `census.cache.import_frozen`, with the same safety rules as the CLI: atomic, and a synthetic import into the default cache needs an explicit flag.

## 7. Release

v0.13.0 with a Zenodo DOI: code review (`/code-review high`), fixes, full gate (`--jobs 0`), PR, CI, merge, tag, Zenodo confirmation.

## 8. Defaults taken without asking (owner may override)

| ID | Default |
| :--- | :--- |
| D14-1 | Multi-state manifests stack states; cross-state models need an explicit state term in the formula (no automatic fixed effects) |
| D14-2 | RPP year defaults to the latest bundled year, and the manifest records it. A mismatch with the ACS period is a note, not a blocking deviation |
| D14-3 | The Michigan fixtures are committed if the fixture set stays under about 5 MB in total; otherwise the agent reports the size first |

---

## 9. Implementation status (2026-11-08)

| Slice | Result |
| :--- | :--- |
| A multi-state manifests | `states`, derivations on replicates, declared RPP `adjustments` (one shared implementation with `place_adjust_frame`). Live MI/NY confirmatory run: no deviations, identical to Round 13 (811.0 [684.3, 937.7]). Existing manifest hashes unchanged |
| B Michigan | Added to the battery; fixtures 2.0 to 2.2 MB; 10/10 checks pass live and offline; no tolerance changes |
| C PUMS memory (R2) | Measured: about 275 MiB + 2,829 B/row; 1.9M rows peaks at 5.4 GiB after removing avoidable copies (was 6.4). 128 GiB holds every state's records at once. R2 closed |
| D small items | MCP `cache_import`; the only pandas 4 deprecation in census/ fixed |
| Review | `/code-review high`: 10 findings fixed with tests (`tests/test_review_fixes_r14.py`). Parent change: `create_manifest` never overwrites its input |
| Gate | Full `--gate --jobs 0`: PASS, 17 min; coverage table unchanged |

**Deferred:** adjustments limited to one release and not combinable with aggregation; `run_manifest` does not fetch VRE replicates itself; the offline multi-state test uses frozen CBSA membership (the overlay is covered by the live test).
