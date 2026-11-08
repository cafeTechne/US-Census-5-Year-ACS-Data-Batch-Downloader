# Round 13: Real Data, End to End

**Date:** 2026-11-07
**Status:** Implemented (section 11); released as v0.12.0
**Baseline:** v0.11.0 (Round 12 merged; default multilevel/DiD design studies added to the release gate)
**Companions:** [PLAN_ROUND_11.md](PLAN_ROUND_11.md) (R1/R2/F4 risks), [PLAN_ROUND_12.md](PLAN_ROUND_12.md) (deferrals)

## 0. Front-loaded findings (fill before implementation starts)

Items in this section come first in the work order. Each one is either fixed in Round 13 or recorded as a limitation with evidence.

| ID | Source | Finding | Disposition |
| :--- | :--- | :--- | :--- |
| G0 | v0.11.0 full `--gate` run, 2026-11-07 | **PASS.** All 10 required studies are present and every gated row is inside the band. Wall time is **106 min**: `spatial_fay_herriot` takes 56 min (53%) and `regression_calibration` 18 min; the four default design studies add about 11 min combined | No defects. Runtime goes to C3, which targets `spatial_fay_herriot` first |
| G1 | GitHub Actions API, 2026-11-07 | The weekly `live-validation` job has zero scheduled runs. **Cause verified: it is not yet due, not a defect.** The cron was pushed on Monday 2026-11-02 after that day's 06:00 UTC slot, so the first scheduled run is Monday 2026-11-09. The `CENSUS_API_KEY` secret exists and the workflow is active. A first run was started via `workflow_dispatch` on 2026-11-08 UTC | Workstream A1: triage that run; confirm the 2026-11-09 scheduled run |
| G2 | `.github/workflows/*.yml` | CI never runs `scripts/inference_validation.py`, not even `--fast`. A coverage regression would show up only at release time | Workstream C1 |
| G3 | PLAN_ROUND_12 sec. 12 | Deferred: CLI and MCP `run_manifest` cannot take replicates. There is no CLI command to load frozen responses into the cache | Workstream B1, B2 |
| G4 | PLAN_ROUND_11 F4 | The DiD coverage studies use independent shocks only. Serial correlation is untested | Workstream C2 |
| G5 | PLAN_ROUND_11 R1/F3 | R1 (runtime checksum verification) is kept as a documented risk. R2 (PUMS memory) has a static bound only and no measurement | Workstream D |

Rule: do not tune thresholds to make a G0 row pass. Before calling a failing row a fluke, rerun it on a new seed with more replications (as was done for the DiD 0.872 cell in Round 12).

## 1. The improvement this round delivers

Everything shipped so far has been verified offline, against frozen fixtures and synthetic data. Round 13 runs the toolkit against **live Census data**:

- A scheduled and on-demand comparison with Census-published values that actually runs.
- One real-data research workflow, from extraction through a journal-quality map and model to a manifest, that a second reader can rerun.
- The few interface gaps that kept the Round 12 reference workflow Python-only.

No new estimator families, providers, or interfaces. The PLAN_ROUND_12 sec. 10 deferrals still apply.

## 2. Workstreams

| Priority | Workstream | Slices |
| :--- | :--- | :--- |
| P0 | A. Live verification | A1, A2 |
| P0 | B. Close the Round 12 interface gaps | B1, B2 |
| P1 | C. Validation in CI and the DiD sensitivity study | C1, C2 |
| P1 | E. Real-data research workflow | E1, E2 |
| P2 | D. Measured risks (R1/R2) | D1, D2 |

The work order is section 0 items, then A, B, C, E, D. A and B are independent and can run as parallel agents in **separate git worktrees**. C1 touches only CI. E depends on A1, because it needs a working live path.

## 3. Workstream A: live verification (P0)

**A1. Make the live job run.**
- Confirm that the `CENSUS_API_KEY` repository secret exists. Check that the API reports the secret by name only; never read or print its value.
- Trigger `live-validation` through `workflow_dispatch` and find out why the schedule has never fired. Possible causes: the cron is on `defaultBranch`, the workflow file was added recently, or Actions schedules are disabled.
- Triage every failing comparison into one of four buckets:
  - tool defect (fix it);
  - Census revision or errata (record it with its errata ID);
  - tolerance or rounding (document it; don't widen silently);
  - network or flaky (retry policy).
- Done when one green dispatched run exists, its `validation-report` artifact is linked from `docs/audits/validation_report.md`, and the schedule has fired at least once or its blocker is documented.

**A2. Run locally with the owner's key.**
- Run `scripts/validate_against_census.py` from this machine with the key in the environment only.
- Commit the regenerated `docs/audits/validation_report.{md,json}` with its date and vintages.
- Done when the committed report reflects a live run, not a frozen one, and says which.

## 4. Workstream B: close the Round 12 interface gaps (P0)

**B1. Replicates in the CLI and MCP `run_manifest`.**
- Add `replicates_path` to the CLI (`--arg replicates_path=...`) and the MCP tool, reusing the existing `read_replicates`/`replicates_sha256` adapter.
- Remove the "Python route" caveat from `docs/reference_workflow.md`.
- Tests: CLI and MCP parity with the Python route, including the same manifest hash and the strict-policy blocked case.

**B2. Cache import CLI.**
- Add `python -m census cache import <frozen_dir>` (extending the existing `cache` command, not a new runner) that loads frozen responses and marks the cache synthetic when the source is synthetic.
- The reference workflow should then run entirely from the CLI.
- Test: the documented commands run twice with the network refused and produce identical results. Extend `tests/test_reference_workflow.py`; don't add a new harness.

## 5. Workstream C: validation in CI and the DiD sensitivity study (P1)

**C1. Fast gate in CI.**
- Add one CI step, `python scripts/inference_validation.py --fast --gate`, on one OS and one Python version only.
- Measure its runtime first (PLAN_ROUND_12 P12-7). If it costs more than about 5 minutes, run it on `pull_request` to `defaultBranch` only.
- The full gate stays a release-time step, as in the v0.11.0 process.

**C2. DiD serial-correlation sensitivity (F4).**
- Add an `info`-status study with AR(1) unit errors at rho of 0, 0.3, 0.6 and 0.9, for the default DiD intervals.
- It does not gate. It reports where coverage degrades, and documentation states the model scope (independent errors, or clustered SE where supported).
- Promote it to gated only if the owner decides that serially correlated panels are an in-scope claim (D13-3).

**C3. Full-gate runtime.**
- The v0.11.0 full gate took 106 minutes; the script header claimed about 40. `spatial_fay_herriot` alone took 56 minutes (G0), so profile its per-replication fit before anything else.
- Profile the gate per study, then:
  - run independent studies in separate processes;
  - pin BLAS threads per worker so the processes don't oversubscribe the CPU;
  - make sure each study's seed does not depend on scheduling order, so results stay identical.
- Done when a parallel run reproduces the serial CSV exactly at the same seed, the wall time is recorded, and the header gives the measured time.

## 6. Workstream E: real-data research workflow (P1)

**E1. One worked study on live ACS 5-year data.**
- County median household income, adjusted with RPP, for Michigan and New York (D13-2):
  - a choropleth map with an uncertainty layer (journal style);
  - a regression with replicate SEs;
  - a run manifest showing `data_source=census_api`;
  - a results bundle.
- The scope is a single workflow, not a gallery.
- Spot-check its published numbers against data.census.gov through A2.

**E2. Second reader.**
- An independent agent session reruns E1 from the documentation alone, as in Round 12 P12-8. Its verdict and fixes are recorded.
- Done when the rerun reproduces the substantive results and the manifest hash differs only in the documented volatile fields.

## 7. Workstream D: measured risks (P2, cut first if time runs short)

**D1. R1: runtime integrity for bundled price tables.**
- Verify `csv_sha256` when bundled tables load (the files are small, so the cost is negligible).
- Raise an error on a mismatch for bundled data. Supply custom directories with a recorded hash and no verification.
- If the owner keeps R1 as a documented risk (D13-4), skip this slice.

**D2. R2: PUMS memory benchmark.**
- Run a bounded synthetic benchmark that records peak RSS per phase (parse, concatenate, weight matrix) and checks that estimates match across chunk sizes.
- Size: up to an `81 x 1.9M` weight matrix, about 1.15 GiB static, well inside 128 GB.
- Outcome: a documented limit, with no streaming rewrite unless the measurement shows one is needed.

## 8. Release

The round ends as **v0.12.0** with a Zenodo DOI, following the standing release process: version bump, CHANGELOG, full `--gate`, PR, CI, merge, tag, and Zenodo confirmation.

## 9. Owner decisions

| ID | Decision (owner, 2026-11-07) |
| :--- | :--- |
| D13-1 | **Yes.** For our own use, agents use the owner's key (environment only, never printed or committed). When the toolkit is published for others, each user supplies their own `CENSUS_API_KEY`; docs and errors must say so, and nothing ships with a key |
| D13-2 | **Michigan (26) and New York (36)** are the test targets for E1 and the live spot checks; all states remain supported (no state-specific code) |
| D13-3 | **Report only.** The serial-correlation DiD study is `info` status and does not gate |
| D13-4 | **Implement D1** (load-time checksum verification for bundled tables) |
| D13-5 | **PRs only.** The fast gate runs on `pull_request` to `defaultBranch`; the full gate stays release-time only |

## 10. Definition of done

- Section 0 items are resolved or recorded as limitations with evidence.
- At least one green live validation run, with the report committed and labeled live.
- The reference workflow runs from the CLI with no Python steps.
- The fast gate runs in CI. The DiD sensitivity results are documented.
- One real-data workflow is reproduced by a second reader.
- D1 and D2 are done or explicitly deferred.
- v0.12.0 is released with a DOI.

---

## 11. Implementation status (2026-11-07)

| Slice | Result |
| :--- | :--- |
| G0 / gate | v0.11.0 gate PASS (106 min). After C3 the full gate PASSES in 17.2 min (`--jobs 0`), with a coverage table identical to v0.11.0 |
| A1 live CI | First dispatched run: 941 live tests passed; one failure was a stale `expectedFailure` on F5 (fixed 2026-11-02, marker never removed). Fixed, along with the F5 criterion text. Scheduled run due 2026-11-09 |
| A2 live battery | 10/10 checks pass live (`docs/audits/validation_report.md`, 2026-11-08) |
| B interfaces | `cache import` (atomic; synthetic into the default cache requires `--allow-default-cache`; `cache clear` removes the marker). Python, CLI and MCP `run_manifest` parity with `replicates_path`. The reference workflow is CLI only |
| C validation | Fast smoke gate on PRs (band widened by 3 MC SE). `did_ar1_sensitivity` (info): coverage 0.88-0.91 for rho 0 to 0.9 in 4-5 period panels. Pinning BLAS to one thread plus `--jobs` cut spatial FH from 3361 s to about 200 s, and its results no longer depend on the machine's thread count |
| D1 integrity | Bundled price tables are verified against their SHA-256 at load; R1 closed |
| D2 PUMS memory | Not done; deferred (R2 stays documented with its static bound) |
| E1 case study | Live MI/NY study (`docs/case_study_mi_ny.md`): pct_bachelors slope 811 RPP-adjusted dollars per point, 90% CI [684, 938] (population target); 10/10 spot-check counties identical to the API. New tools: `place_adjust_frame`, `make_results_bundle` |
| Review | `/code-review high`: 10 findings, all fixed with regression tests (`tests/test_review_fixes_r13.py`) |
| E2 second reader | Verdict "usable as-is": every command ran and every number matched. Clarity edits applied |

**Deferred:** D2 (PUMS memory benchmark); multi-state `run_manifest` with a place-adjustment step (so the case study is exploratory, not pre-registered); no MCP route for `cache import`; adding Michigan to the validation battery's states (needs fixtures re-recorded).
